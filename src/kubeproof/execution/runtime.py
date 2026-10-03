"""Bounded kind execution for a deliberately acknowledged Helm product."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from kubeproof.core.domain import (
    Assessment,
    CheckResult,
    EnvironmentInfo,
    ExecutionStatus,
    Finding,
    Observation,
    ResourceRef,
    Severity,
    SourceClass,
)
from kubeproof.core.plans import PlanExecution
from kubeproof.core.profile import CompanyProfile
from kubeproof.execution.environment import EnvironmentError, KindProvider, temporary_kubeconfig
from kubeproof.execution.helm import HelmInstaller, HelmRenderError, HelmRenderRequest
from kubeproof.execution.infrastructure import (
    METRICS_SERVER_MANIFEST,
    InfrastructureError,
    install_metrics_server,
)
from kubeproof.execution.kubernetes import (
    ClusterReader,
    KubernetesError,
    wait_for_deployments,
    wait_for_workloads,
)
from kubeproof.execution.runtime_checks import RUNTIME_TITLES as _RUNTIME_TITLES
from kubeproof.execution.runtime_checks import measure_recovery as _measure_recovery
from kubeproof.execution.runtime_checks import measure_resources as _measure_resources
from kubeproof.execution.runtime_checks import observe_dns as _observe_dns
from kubeproof.execution.runtime_checks import untested as _untested
from kubeproof.execution.runtime_monitor import (
    MetricsCollector as _MetricsCollector,
)
from kubeproof.execution.runtime_monitor import (
    SafetyMonitor as _SafetyMonitor,
)
from kubeproof.execution.runtime_monitor import UnsafeWorkloadError


@dataclass(frozen=True)
class RuntimeResult:
    checks: tuple[CheckResult, ...]
    observations: tuple[Observation, ...]
    findings: tuple[Finding, ...]
    environment: EnvironmentInfo
    artifacts: dict[str, str]
    plan_execution: PlanExecution | None = None


@dataclass(frozen=True)
class ExperimentEvidence:
    checks: tuple[CheckResult, ...]
    observations: tuple[Observation, ...]
    findings: tuple[Finding, ...]
    artifacts: dict[str, str]


@dataclass
class _RuntimeEvidence:
    checks: dict[str, CheckResult] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    experiment_checks: list[CheckResult] = field(default_factory=list)

    def observe(
        self,
        check_id: str,
        kind_name: str,
        summary: str,
        *,
        resource: ResourceRef | None = None,
        data: dict[str, object] | None = None,
    ) -> Observation:
        observation = Observation(
            id=f"runtime-obs-{len(self.observations) + 1:05d}",
            check_id=check_id,
            source_class=SourceClass.RUNTIME,
            observation_type=kind_name,
            summary=summary,
            resource=resource,
            data=data or {},
        )
        self.observations.append(observation)
        return observation

    def add_experiment(self, result: ExperimentEvidence) -> None:
        for check in result.checks:
            if check.id in _RUNTIME_TITLES:
                self.checks[check.id] = check
            else:
                self.experiment_checks.append(check)
        self.observations.extend(result.observations)
        self.findings.extend(result.findings)
        self.artifacts.update(result.artifacts)


class RuntimeExperiment(Protocol):
    check_id: str
    title: str

    def validate_resources(
        self, resources: tuple[dict[str, Any], ...], request: HelmRenderRequest
    ) -> None: ...

    def prepare(self, cluster_name: str, kubeconfig: Path) -> None: ...

    def run(
        self,
        cluster: ClusterReader,
        kubeconfig: Path,
        namespace: str,
        cancel_if: Callable[[], bool],
    ) -> ExperimentEvidence: ...


def _remaining_checks(
    checks: dict[str, CheckResult],
    explanation: str,
    *,
    failed: bool,
    cancelled: bool = False,
) -> None:
    for check_id, title in _RUNTIME_TITLES.items():
        checks.setdefault(
            check_id, _untested(check_id, title, explanation, failed=failed, cancelled=cancelled)
        )


def _workload_targets(
    resources: tuple[dict[str, Any], ...], default_namespace: str
) -> tuple[set[tuple[str, str, str]], set[tuple[str, str]]]:
    """Identify rendered workloads, including operator-created StatefulSets."""
    workload_names = {
        (
            str(item.get("kind")),
            str(item.get("metadata", {}).get("namespace") or default_namespace),
            str(item.get("metadata", {}).get("name")),
        )
        for item in resources
        if item.get("kind") in {"Deployment", "StatefulSet", "DaemonSet", "Job"}
        and "helm.sh/hook" not in item.get("metadata", {}).get("annotations", {})
    }
    for item in resources:
        prefix = {"Prometheus": "prometheus", "Alertmanager": "alertmanager"}.get(
            str(item.get("kind"))
        )
        if prefix:
            metadata = item.get("metadata", {})
            workload_names.add(
                (
                    "StatefulSet",
                    str(metadata.get("namespace") or default_namespace),
                    f"{prefix}-{metadata.get('name')}",
                )
            )
    deployment_names = {
        (
            str(item.get("metadata", {}).get("namespace") or default_namespace),
            str(item.get("metadata", {}).get("name")),
        )
        for item in resources
        if item.get("kind") == "Deployment"
    }
    return workload_names, deployment_names


def _installation_infrastructure_error(cluster: ClusterReader, namespace: str) -> bool:
    """Distinguish an unusable cluster from a chart that did not become Ready."""
    try:
        return any(
            event.get("reason")
            in {
                "FailedPull",
                "FailedScheduling",
                "FailedMount",
                "FailedCreatePodSandBox",
                "FailedAttachVolume",
            }
            or "failed to pull image" in str(event.get("message", "")).lower()
            or "no space left on device" in str(event.get("message", "")).lower()
            for event in cluster.events(namespace)
        )
    except KubernetesError:
        return True


def _installation_result(
    observation: Observation,
    *,
    install_error: str | None,
    ready: bool,
    has_workloads: bool,
    infrastructure_error: bool,
) -> tuple[CheckResult, Finding | None]:
    passed = install_error is None and ready
    finding = (
        Finding(
            id="runtime-finding-00001",
            check_id="runtime.installation",
            severity=Severity.BLOCKER,
            title="Installation or supported workload readiness failed",
            description=(
                "The cluster API remained healthy, but Helm or a rendered workload "
                "did not become ready within the deadline."
            ),
            observation_ids=(observation.id,),
        )
        if not passed and not infrastructure_error and (install_error is not None or has_workloads)
        else None
    )
    return (
        CheckResult(
            id="runtime.installation",
            title=_RUNTIME_TITLES["runtime.installation"],
            execution_status=(
                ExecutionStatus.INFRASTRUCTURE_FAILURE
                if infrastructure_error
                else ExecutionStatus.COMPLETED
            ),
            assessment=(
                Assessment.NOT_TESTED
                if infrastructure_error
                else Assessment.PASS
                if passed
                else Assessment.INCONCLUSIVE
                if not has_workloads and install_error is None
                else Assessment.FAIL
            ),
            observation_ids=(observation.id,),
            finding_ids=(finding.id,) if finding else (),
        ),
        finding,
    )


def _collect_product_artifacts(
    cluster: ClusterReader, namespace: str, workload_statuses: list[dict[str, Any]]
) -> dict[str, str]:
    try:
        return {
            "artifacts/kubernetes/pods.json": json.dumps(
                cluster.pods(namespace), indent=2, sort_keys=True
            )
            + "\n",
            "artifacts/kubernetes/events.json": json.dumps(
                cluster.events(namespace), indent=2, sort_keys=True
            )
            + "\n",
            "artifacts/kubernetes/workloads.json": json.dumps(
                workload_statuses, indent=2, sort_keys=True
            )
            + "\n",
            "artifacts/installation/logs.json": json.dumps(
                cluster.product_logs(namespace), indent=2, sort_keys=True
            )
            + "\n",
        }
    except KubernetesError as exc:
        return {"artifacts/kubernetes/collection-error.txt": str(exc) + "\n"}


def _cleanup_runtime(
    *,
    kind: KindProvider,
    helm: HelmInstaller,
    request: HelmRenderRequest,
    kubeconfig: Path,
    cluster: ClusterReader | None,
    cluster_name: str,
    install_attempted: bool,
) -> tuple[str | None, tuple[str, ...]]:
    """Best-effort release and cluster removal after a create attempt."""
    cleanup_error: str | None = None
    leftovers: tuple[str, ...] = ()
    if install_attempted:
        try:
            helm.uninstall(request.release_name, request.namespace, kubeconfig)
        except HelmRenderError as exc:
            cleanup_error = str(exc)
        if cluster is not None:
            try:
                deadline = time.monotonic() + 30
                while True:
                    pod_names = tuple(
                        sorted(
                            f"Pod/{item.get('metadata', {}).get('name')}"
                            for item in cluster.pods(request.namespace)
                        )
                    )
                    if not pod_names or time.monotonic() >= deadline:
                        leftovers = pod_names
                        break
                    time.sleep(2)
            except KubernetesError as exc:
                cleanup_error = f"{cleanup_error}; {exc}" if cleanup_error else str(exc)
    try:
        kind.delete(cluster_name)
    except EnvironmentError as exc:
        cleanup_error = f"{cleanup_error}; {exc}" if cleanup_error else str(exc)
    return cleanup_error, leftovers


@dataclass(frozen=True)
class _Installation:
    ready: bool
    started: float
    workload_statuses: list[dict[str, Any]]
    deployments: list[dict[str, Any]]
    check: CheckResult
    finding: Finding | None


def _install_product(
    request: HelmRenderRequest,
    resources: tuple[dict[str, Any], ...],
    helm: HelmInstaller,
    kubeconfig: Path,
    cluster: ClusterReader,
    monitor: _SafetyMonitor,
    timeout: int,
    observe: Callable[..., Observation],
) -> _Installation:
    started = time.monotonic()
    install_error: str | None = None
    try:
        helm.install(request, kubeconfig, timeout, cancel_if=monitor.triggered.is_set)
    except HelmRenderError as exc:
        install_error = str(exc)
    if monitor.triggered.is_set():
        if monitor.infrastructure_error:
            raise KubernetesError(monitor.reason or "dynamic safety monitoring failed")
        raise UnsafeWorkloadError(monitor.reason or "dynamic Pod violated local safety policy")
    elapsed = round(time.monotonic() - started, 3)
    if not cluster.healthy():
        raise KubernetesError("cluster API became unavailable during Helm installation")

    workload_names, deployment_names = _workload_targets(resources, request.namespace)
    ready, workload_statuses = (
        wait_for_workloads(cluster, workload_names, timeout=max(1, timeout - int(elapsed)))
        if workload_names and install_error is None
        else (False, [])
    )
    deployments = [
        item
        for item in cluster.deployments()
        if (
            item.get("metadata", {}).get("namespace"),
            item.get("metadata", {}).get("name"),
        )
        in deployment_names
    ]
    infrastructure_error = (
        _installation_infrastructure_error(cluster, request.namespace)
        if install_error is not None
        else False
    )
    observation = observe(
        "runtime.installation",
        "installation.helm_and_readiness",
        "Helm installation and supported workload readiness were observed.",
        data={
            "helm_succeeded": install_error is None,
            "helm_error": install_error,
            "infrastructure_uncertainty": infrastructure_error,
            "duration_seconds": elapsed,
            "workloads_expected": len(workload_names),
            "workloads_seen": len(workload_statuses),
            "workloads_ready": ready,
        },
    )
    check, finding = _installation_result(
        observation,
        install_error=install_error,
        ready=ready,
        has_workloads=bool(workload_names),
        infrastructure_error=infrastructure_error,
    )
    return _Installation(
        install_error is None and ready, started, workload_statuses, deployments, check, finding
    )


def _run_experiment(
    experiment: RuntimeExperiment,
    cluster: ClusterReader,
    kubeconfig: Path,
    namespace: str,
    monitor: _SafetyMonitor,
    installation_ready: bool,
    metrics_error: str | None,
    collector: _MetricsCollector | None,
) -> ExperimentEvidence:
    if not installation_ready:
        reason = "Installation did not reach a ready baseline."
    elif getattr(experiment, "requires_metrics", True) and (
        metrics_error is not None or collector is None
    ):
        reason = metrics_error or "Metrics collector did not start"
    else:
        reason = None
    if reason is not None:
        if hasattr(experiment, "not_run"):
            return experiment.not_run(reason, failed=installation_ready)  # type: ignore[no-any-return]
        return ExperimentEvidence(
            (
                _untested(
                    experiment.check_id,
                    experiment.title,
                    reason,
                    failed=installation_ready,
                ),
            ),
            (),
            (),
            {},
        )
    try:
        return experiment.run(cluster, kubeconfig, namespace, monitor.triggered.is_set)
    except UnsafeWorkloadError as exc:
        if monitor.infrastructure_error:
            raise KubernetesError(monitor.reason or "dynamic workload monitor failed") from exc
        raise
    except (KubernetesError, HelmRenderError, OSError, ValueError) as exc:
        return ExperimentEvidence(
            (_untested(experiment.check_id, experiment.title, str(exc), failed=True),),
            (),
            (),
            {},
        )


def run_runtime(
    *,
    request: HelmRenderRequest,
    resources: tuple[dict[str, Any], ...],
    profile: CompanyProfile,
    install_timeout_seconds: int,
    steady_state_seconds: int,
    max_recovery_targets: int,
    provider: KindProvider | None = None,
    installer: HelmInstaller | None = None,
    cluster_factory: Callable[[Path], ClusterReader] = ClusterReader,
    experiment: RuntimeExperiment | None = None,
    approved_host_rule_ids: tuple[str, ...] = (),
) -> RuntimeResult:
    """Run one disposable evaluation; always attempt cluster teardown after create."""
    kind = provider or KindProvider()
    helm = installer or HelmInstaller()
    cluster_name = f"kubeproof-{uuid.uuid4().hex[:12]}"
    evidence = _RuntimeEvidence()
    cleanup_error: str | None = None
    leftovers: tuple[str, ...] = ()
    creation_attempted = False
    install_attempted = False
    metrics_error: str | None = None
    dns_error: str | None = None
    kubernetes_server_version: str | None = None
    directory, kubeconfig = temporary_kubeconfig()
    cluster: ClusterReader | None = None
    collector: _MetricsCollector | None = None
    monitor: _SafetyMonitor | None = None

    try:
        creation_attempted = True
        kind.create(cluster_name, kubeconfig)
        cluster = cluster_factory(kubeconfig)
        if not cluster.healthy():
            raise KubernetesError("new kind cluster API is unavailable")
        if experiment is not None:
            experiment.prepare(cluster_name, kubeconfig)
        if isinstance(cluster, ClusterReader):
            with suppress(KubernetesError):
                kubernetes_server_version = cluster.server_version()
        monitor = _SafetyMonitor(cluster, request.namespace, profile, approved_host_rule_ids)
        monitor.start()
        try:
            install_metrics_server(kubeconfig, cluster)
        except InfrastructureError as exc:
            metrics_error = str(exc)
        try:
            cluster.enable_dns_query_logs()
            dns_ready, _ = wait_for_deployments(cluster, {("kube-system", "coredns")}, timeout=90)
            if not dns_ready:
                dns_error = "CoreDNS did not become Ready after enabling query logs"
        except KubernetesError as exc:
            dns_error = str(exc)
        if metrics_error is None and not getattr(experiment, "owns_runtime_checks", False):
            collector = _MetricsCollector(cluster, request.namespace)
            collector.start()
        install_attempted = True
        installation = _install_product(
            request,
            resources,
            helm,
            kubeconfig,
            cluster,
            monitor,
            install_timeout_seconds,
            evidence.observe,
        )
        evidence.checks["runtime.installation"] = installation.check
        if installation.finding is not None:
            evidence.findings.append(installation.finding)
        if experiment is not None:
            if hasattr(experiment, "configure_runtime"):
                experiment.configure_runtime(
                    monitor=monitor,
                    deployments=installation.deployments,
                    metrics_error=metrics_error,
                    dns_error=dns_error,
                )
            experiment_result = _run_experiment(
                experiment,
                cluster,
                kubeconfig,
                request.namespace,
                monitor,
                installation.ready,
                metrics_error,
                collector,
            )
            evidence.add_experiment(experiment_result)
        if getattr(experiment, "owns_runtime_checks", False):
            _remaining_checks(evidence.checks, "Not selected in the frozen plan.", failed=False)
        elif not installation.ready:
            if collector is not None:
                collector.stop()
            _remaining_checks(
                evidence.checks,
                "Installation did not reach a ready baseline.",
                failed=installation.check.execution_status
                is ExecutionStatus.INFRASTRUCTURE_FAILURE,
            )
        else:
            if collector is not None:
                if monitor.triggered.wait(steady_state_seconds):
                    if monitor.infrastructure_error:
                        raise KubernetesError(monitor.reason or "dynamic safety monitoring failed")
                    raise UnsafeWorkloadError(
                        monitor.reason or "dynamic Pod violated local safety policy"
                    )
                collector.stop()
                metrics_error = collector.error
            if metrics_error or collector is None:
                evidence.checks["runtime.resources"] = _untested(
                    "runtime.resources",
                    _RUNTIME_TITLES["runtime.resources"],
                    metrics_error or "Metrics collector did not start",
                    failed=True,
                )
            else:
                _measure_resources(
                    collector.snapshots,
                    request.namespace,
                    profile,
                    installation.started,
                    evidence.observe,
                    evidence.checks,
                    evidence.findings,
                )
            if dns_error:
                evidence.checks["runtime.network"] = _untested(
                    "runtime.network",
                    _RUNTIME_TITLES["runtime.network"],
                    dns_error,
                    failed=True,
                )
            else:
                _observe_dns(
                    cluster,
                    request.namespace,
                    profile,
                    evidence.observe,
                    evidence.checks,
                    evidence.findings,
                    evidence.artifacts,
                )
            _measure_recovery(
                cluster,
                installation.deployments,
                profile,
                max_recovery_targets,
                evidence.observe,
                evidence.checks,
                evidence.findings,
                monitor,
            )
        evidence.artifacts.update(
            _collect_product_artifacts(cluster, request.namespace, installation.workload_statuses)
        )
    except UnsafeWorkloadError as exc:
        _remaining_checks(evidence.checks, str(exc), failed=False, cancelled=True)
        if (
            experiment is not None
            and hasattr(experiment, "not_run")
            and not getattr(experiment, "execution", None)
        ):
            evidence.add_experiment(experiment.not_run(str(exc), cancelled=True))
        elif experiment is not None and not evidence.experiment_checks:
            evidence.experiment_checks.append(
                _untested(experiment.check_id, experiment.title, str(exc), cancelled=True)
            )
    except (EnvironmentError, KubernetesError, OSError) as exc:
        _remaining_checks(evidence.checks, str(exc), failed=True)
        if (
            experiment is not None
            and hasattr(experiment, "not_run")
            and not getattr(experiment, "execution", None)
        ):
            evidence.add_experiment(experiment.not_run(str(exc), failed=True))
        elif experiment is not None and not evidence.experiment_checks:
            evidence.experiment_checks.append(
                _untested(experiment.check_id, experiment.title, str(exc), failed=True)
            )
    finally:
        if monitor is not None:
            monitor.stop()
        if collector is not None:
            collector.stop()
        if creation_attempted:
            cleanup_error, leftovers = _cleanup_runtime(
                kind=kind,
                helm=helm,
                request=request,
                kubeconfig=kubeconfig,
                cluster=cluster,
                cluster_name=cluster_name,
                install_attempted=install_attempted,
            )
        directory.cleanup()

    return RuntimeResult(
        checks=tuple(evidence.checks[key] for key in _RUNTIME_TITLES)
        + tuple(evidence.experiment_checks),
        observations=tuple(evidence.observations),
        findings=tuple(evidence.findings),
        environment=EnvironmentInfo(
            provider="kind",
            cluster_name=cluster_name,
            cleanup_attempted=creation_attempted,
            cleanup_succeeded=creation_attempted and cleanup_error is None,
            cleanup_error=cleanup_error,
            leftovers_before_cluster_deletion=leftovers,
            runtime_safety_rule_ids=monitor.matched_rule_ids if monitor else (),
            kubernetes_server_version=kubernetes_server_version,
            metrics_server_manifest_sha256=hashlib.sha256(
                METRICS_SERVER_MANIFEST.read_bytes()
            ).hexdigest(),
        ),
        artifacts=evidence.artifacts,
        plan_execution=getattr(experiment, "execution", None),
    )
