"""Bounded kind execution for a deliberately acknowledged Helm product."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kubeproof.analysis import analyze
from kubeproof.domain import (
    AdmissionOutcome,
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
from kubeproof.environment import EnvironmentError, KindProvider, temporary_kubeconfig
from kubeproof.helm import HelmInstaller, HelmRenderError, HelmRenderRequest
from kubeproof.infrastructure import InfrastructureError, install_metrics_server
from kubeproof.kubernetes import (
    ClusterReader,
    KubernetesError,
    wait_for_deployments,
    wait_for_workloads,
)
from kubeproof.profile import CompanyProfile
from kubeproof.quantities import InvalidQuantity, parse_quantity
from kubeproof.safety import decide_local_admission


@dataclass(frozen=True)
class RuntimeResult:
    checks: tuple[CheckResult, ...]
    observations: tuple[Observation, ...]
    findings: tuple[Finding, ...]
    environment: EnvironmentInfo
    artifacts: dict[str, str]


def _untested(
    check_id: str,
    title: str,
    explanation: str,
    *,
    failed: bool = False,
    cancelled: bool = False,
) -> CheckResult:
    return CheckResult(
        id=check_id,
        title=title,
        execution_status=(
            ExecutionStatus.INFRASTRUCTURE_FAILURE
            if failed
            else ExecutionStatus.CANCELLED
            if cancelled
            else ExecutionStatus.COMPLETED
        ),
        assessment=Assessment.NOT_TESTED,
        explanation=explanation,
    )


_RUNTIME_TITLES = {
    "runtime.installation": "Installation and readiness",
    "runtime.resources": "Observed resource consumption",
    "runtime.recovery": "Pod replacement readiness",
    "runtime.network": "Observed network and DNS behavior",
}


@dataclass(frozen=True)
class _MetricsSnapshot:
    captured_at: float
    pods: list[dict[str, Any]]
    metrics: list[dict[str, Any]]


class _MetricsCollector:
    """Collect raw Metrics API snapshots while Helm blocks on installation."""

    def __init__(self, cluster: ClusterReader, namespace: str) -> None:
        self.cluster = cluster
        self.namespace = namespace
        self.snapshots: list[_MetricsSnapshot] = []
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._collect, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            self.error = "Metrics collector did not stop before its deadline"

    def _collect(self) -> None:
        while not self._stop.is_set():
            try:
                pods = self.cluster.pods(self.namespace)
                metrics = self.cluster.pod_metrics(self.namespace)
                if len(self.snapshots) >= 120:
                    self.error = "Metrics collector exceeded the 120-snapshot safety limit"
                    return
                self.snapshots.append(_MetricsSnapshot(time.monotonic(), pods, metrics))
            except Exception as exc:
                self.error = str(exc)
                return
            self._stop.wait(15)


class UnsafeWorkloadError(RuntimeError):
    pass


class _SafetyMonitor:
    """Poll dynamically created Pod specs and stop at a local hard rule."""

    def __init__(self, cluster: ClusterReader, namespace: str, profile: CompanyProfile) -> None:
        self.cluster = cluster
        self.namespace = namespace
        self.profile = profile
        self.triggered = threading.Event()
        self._stop = threading.Event()
        self.reason: str | None = None
        self.infrastructure_error = False
        self.matched_rule_ids: tuple[str, ...] = ()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=20)
        if self._thread.is_alive():
            self.infrastructure_error = True
            self.reason = "dynamic workload monitor did not stop before its deadline"
            self.triggered.set()

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                for pod in self.cluster.pods(self.namespace):
                    metadata = pod.get("metadata", {})
                    manifest = {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": {
                            "name": str(metadata.get("name", "unknown")),
                            "namespace": self.namespace,
                        },
                        "spec": pod.get("spec", {}),
                    }
                    decision = decide_local_admission(
                        analyze((manifest,), self.profile).observations
                    )
                    if decision.outcome is AdmissionOutcome.STATIC_ONLY:
                        self.matched_rule_ids = decision.matched_rule_ids
                        self.reason = (
                            f"dynamic Pod {metadata.get('name')} matched local safety rules: "
                            + ", ".join(decision.matched_rule_ids)
                        )
                        self.triggered.set()
                        return
            except Exception as exc:
                self.infrastructure_error = True
                self.reason = f"dynamic workload monitor failed: {exc}"
                self.triggered.set()
                return
            self._stop.wait(2)


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


def _selector(labels: dict[str, str]) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


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
) -> RuntimeResult:
    """Run one disposable evaluation; always attempt cluster teardown after create."""
    kind = provider or KindProvider()
    helm = installer or HelmInstaller()
    cluster_name = f"kubeproof-{uuid.uuid4().hex[:12]}"
    checks: dict[str, CheckResult] = {}
    observations: list[Observation] = []
    findings: list[Finding] = []
    artifacts: dict[str, str] = {}
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

    def observe(
        check_id: str,
        kind_name: str,
        summary: str,
        *,
        resource: ResourceRef | None = None,
        data: dict[str, object] | None = None,
    ) -> Observation:
        observation = Observation(
            id=f"runtime-obs-{len(observations) + 1:05d}",
            check_id=check_id,
            source_class=SourceClass.RUNTIME,
            observation_type=kind_name,
            summary=summary,
            resource=resource,
            data=data or {},
        )
        observations.append(observation)
        return observation

    try:
        creation_attempted = True
        kind.create(cluster_name, kubeconfig)
        cluster = cluster_factory(kubeconfig)
        if not cluster.healthy():
            raise KubernetesError("new kind cluster API is unavailable")
        if isinstance(cluster, ClusterReader):
            with suppress(KubernetesError):
                kubernetes_server_version = cluster.server_version()
        monitor = _SafetyMonitor(cluster, request.namespace, profile)
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
        if metrics_error is None:
            collector = _MetricsCollector(cluster, request.namespace)
            collector.start()
        started = time.monotonic()
        install_error: str | None = None
        try:
            install_attempted = True
            helm.install(
                request,
                kubeconfig,
                install_timeout_seconds,
                cancel_if=monitor.triggered.is_set,
            )
        except HelmRenderError as exc:
            install_error = str(exc)
        if monitor.triggered.is_set():
            if monitor.infrastructure_error:
                raise KubernetesError(monitor.reason or "dynamic safety monitoring failed")
            raise UnsafeWorkloadError(monitor.reason or "dynamic Pod violated local safety policy")
        elapsed = round(time.monotonic() - started, 3)
        if not cluster.healthy():
            raise KubernetesError("cluster API became unavailable during Helm installation")

        workload_names = {
            (
                str(item.get("kind")),
                str(item.get("metadata", {}).get("namespace") or request.namespace),
                str(item.get("metadata", {}).get("name")),
            )
            for item in resources
            if item.get("kind") in {"Deployment", "StatefulSet", "DaemonSet", "Job"}
            and "helm.sh/hook" not in item.get("metadata", {}).get("annotations", {})
        }
        # Prometheus Operator creates these StatefulSets from chart CRs after Helm
        # has submitted the manifests. They are product workloads, not infrastructure.
        for item in resources:
            prefix = {"Prometheus": "prometheus", "Alertmanager": "alertmanager"}.get(
                str(item.get("kind"))
            )
            if prefix:
                metadata = item.get("metadata", {})
                workload_names.add(
                    (
                        "StatefulSet",
                        str(metadata.get("namespace") or request.namespace),
                        f"{prefix}-{metadata.get('name')}",
                    )
                )
        deployment_names = {
            (
                str(item.get("metadata", {}).get("namespace") or request.namespace),
                str(item.get("metadata", {}).get("name")),
            )
            for item in resources
            if item.get("kind") == "Deployment"
        }
        ready, workload_statuses = (
            wait_for_workloads(
                cluster, workload_names, timeout=max(1, install_timeout_seconds - int(elapsed))
            )
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
        infrastructure_install_error = False
        if install_error is not None:
            try:
                infrastructure_install_error = any(
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
                    for event in cluster.events(request.namespace)
                )
            except KubernetesError:
                infrastructure_install_error = True
        installation = observe(
            "runtime.installation",
            "installation.helm_and_readiness",
            "Helm installation and supported workload readiness were observed.",
            data={
                "helm_succeeded": install_error is None,
                "helm_error": install_error,
                "infrastructure_uncertainty": infrastructure_install_error,
                "duration_seconds": elapsed,
                "workloads_expected": len(workload_names),
                "workloads_seen": len(workload_statuses),
                "workloads_ready": ready,
            },
        )
        passed = install_error is None and ready
        if (
            not passed
            and not infrastructure_install_error
            and (install_error is not None or workload_names)
        ):
            findings.append(
                Finding(
                    id="runtime-finding-00001",
                    check_id="runtime.installation",
                    severity=Severity.BLOCKER,
                    title="Installation or supported workload readiness failed",
                    description=(
                        "The cluster API remained healthy, but Helm or a rendered workload "
                        "did not become ready within the deadline."
                    ),
                    observation_ids=(installation.id,),
                )
            )
        checks["runtime.installation"] = CheckResult(
            id="runtime.installation",
            title=_RUNTIME_TITLES["runtime.installation"],
            execution_status=(
                ExecutionStatus.INFRASTRUCTURE_FAILURE
                if infrastructure_install_error
                else ExecutionStatus.COMPLETED
            ),
            assessment=(
                Assessment.NOT_TESTED
                if infrastructure_install_error
                else Assessment.PASS
                if passed
                else Assessment.INCONCLUSIVE
                if not workload_names and install_error is None
                else Assessment.FAIL
            ),
            observation_ids=(installation.id,),
            finding_ids=tuple(
                item.id for item in findings if item.check_id == "runtime.installation"
            ),
        )
        if not passed:
            if collector is not None:
                collector.stop()
            _remaining_checks(
                checks,
                "Installation did not reach a ready baseline.",
                failed=infrastructure_install_error,
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
                checks["runtime.resources"] = _untested(
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
                    started,
                    observe,
                    checks,
                    findings,
                )
            if dns_error:
                checks["runtime.network"] = _untested(
                    "runtime.network",
                    _RUNTIME_TITLES["runtime.network"],
                    dns_error,
                    failed=True,
                )
            else:
                _observe_dns(
                    cluster, request.namespace, profile, observe, checks, findings, artifacts
                )
            _measure_recovery(
                cluster,
                deployments,
                profile,
                max_recovery_targets,
                observe,
                checks,
                findings,
                monitor,
            )
        try:
            artifacts["artifacts/kubernetes/pods.json"] = (
                json.dumps(cluster.pods(request.namespace), indent=2, sort_keys=True) + "\n"
            )
            artifacts["artifacts/kubernetes/events.json"] = (
                json.dumps(cluster.events(request.namespace), indent=2, sort_keys=True) + "\n"
            )
            artifacts["artifacts/kubernetes/workloads.json"] = (
                json.dumps(workload_statuses, indent=2, sort_keys=True) + "\n"
            )
            artifacts["artifacts/installation/logs.json"] = (
                json.dumps(cluster.product_logs(request.namespace), indent=2, sort_keys=True) + "\n"
            )
        except KubernetesError as exc:
            artifacts["artifacts/kubernetes/collection-error.txt"] = str(exc) + "\n"
    except UnsafeWorkloadError as exc:
        _remaining_checks(checks, str(exc), failed=False, cancelled=True)
    except (EnvironmentError, KubernetesError, OSError) as exc:
        _remaining_checks(checks, str(exc), failed=True)
    finally:
        if monitor is not None:
            monitor.stop()
        if collector is not None:
            collector.stop()
        if creation_attempted:
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
        directory.cleanup()

    return RuntimeResult(
        checks=tuple(checks[key] for key in _RUNTIME_TITLES),
        observations=tuple(observations),
        findings=tuple(findings),
        environment=EnvironmentInfo(
            provider="kind",
            cluster_name=cluster_name,
            cleanup_attempted=creation_attempted,
            cleanup_succeeded=creation_attempted and cleanup_error is None,
            cleanup_error=cleanup_error,
            leftovers_before_cluster_deletion=leftovers,
            runtime_safety_rule_ids=monitor.matched_rule_ids if monitor else (),
            kubernetes_server_version=kubernetes_server_version,
        ),
        artifacts=artifacts,
    )


def _measure_resources(
    snapshots: list[_MetricsSnapshot],
    namespace: str,
    profile: CompanyProfile,
    origin: float,
    observe: Callable[..., Observation],
    checks: dict[str, CheckResult],
    findings: list[Finding],
) -> None:
    seen: set[tuple[str, str]] = set()
    samples: list[Observation] = []
    incomplete = False
    try:
        for snapshot in snapshots:
            live_pods = {str(item.get("metadata", {}).get("name")): item for item in snapshot.pods}
            for pod_metrics in snapshot.metrics:
                metadata = pod_metrics.get("metadata", {})
                pod_name = str(metadata.get("name", ""))
                pod = live_pods.get(pod_name)
                if pod is None:
                    incomplete = True
                    continue
                pod_uid = str(pod.get("metadata", {}).get("uid", ""))
                timestamp = str(pod_metrics.get("timestamp", ""))
                identity = (pod_uid, timestamp)
                if identity in seen:
                    continue
                seen.add(identity)
                expected = {
                    str(item.get("name")) for item in pod.get("spec", {}).get("containers", [])
                }
                containers = pod_metrics.get("containers", [])
                measured = {str(item.get("name")) for item in containers}
                complete = (
                    bool(expected)
                    and expected <= measured
                    and all(
                        key in item.get("usage", {})
                        for item in containers
                        for key in ("cpu", "memory")
                    )
                )
                incomplete = incomplete or not complete
                totals = {
                    key: sum(
                        (
                            parse_quantity(str(item.get("usage", {}).get(key, "0")))
                            for item in containers
                        ),
                        start=0,
                    )
                    for key in ("cpu", "memory")
                }
                sample = observe(
                    "runtime.resources",
                    "resources.pod_metrics_sample",
                    "Kubernetes Metrics API returned a Pod CPU/memory sample.",
                    resource=ResourceRef(
                        api_version="v1", kind="Pod", namespace=namespace, name=pod_name
                    ),
                    data={
                        "pod_uid": pod_uid,
                        "timestamp": timestamp,
                        "window": pod_metrics.get("window"),
                        "collection_elapsed_seconds": round(snapshot.captured_at - origin, 3),
                        "sampling_interval_seconds": 15,
                        "containers_expected": sorted(expected),
                        "containers_measured": sorted(measured),
                        "complete": complete,
                        "cpu_base_units": str(totals["cpu"]),
                        "memory_base_units": str(totals["memory"]),
                    },
                )
                samples.append(sample)
                if complete:
                    for key, threshold in (
                        ("cpu", profile.constraints.resources.max_sampled_cpu_per_pod),
                        ("memory", profile.constraints.resources.max_sampled_memory_per_pod),
                    ):
                        if threshold and totals[key] > parse_quantity(threshold):
                            findings.append(
                                Finding(
                                    id=f"runtime-finding-{len(findings) + 1:05d}",
                                    check_id="runtime.resources",
                                    severity=Severity.BLOCKER,
                                    title=f"Observed Pod {key} sample exceeds profile threshold",
                                    description=f"A complete Pod sample exceeded {threshold}.",
                                    observation_ids=(sample.id,),
                                    constraint=f"constraints.resources.max_sampled_{key}_per_pod",
                                )
                            )
    except InvalidQuantity as exc:
        checks["runtime.resources"] = _untested(
            "runtime.resources", _RUNTIME_TITLES["runtime.resources"], str(exc), failed=True
        )
        return
    checks["runtime.resources"] = CheckResult(
        id="runtime.resources",
        title=_RUNTIME_TITLES["runtime.resources"],
        execution_status=ExecutionStatus.COMPLETED,
        assessment=Assessment.FAIL
        if any(item.check_id == "runtime.resources" for item in findings)
        else (Assessment.INCONCLUSIVE if incomplete or not samples else Assessment.PASS),
        observation_ids=tuple(item.id for item in samples),
        finding_ids=tuple(item.id for item in findings if item.check_id == "runtime.resources"),
        explanation=(
            "No complete product Pod sample was collected." if incomplete or not samples else None
        ),
    )


def _measure_recovery(
    cluster: ClusterReader,
    deployments: list[dict[str, Any]],
    profile: CompanyProfile,
    maximum: int,
    observe: Callable[..., Observation],
    checks: dict[str, CheckResult],
    findings: list[Finding],
    monitor: _SafetyMonitor,
) -> None:
    samples: list[Observation] = []
    limit = profile.constraints.reliability.max_pod_replacement_ready_seconds or 60
    try:
        for deployment in deployments[:maximum]:
            if monitor.triggered.is_set():
                if monitor.infrastructure_error:
                    raise KubernetesError(monitor.reason or "dynamic safety monitoring failed")
                raise UnsafeWorkloadError(
                    monitor.reason or "dynamic Pod violated local safety policy"
                )
            metadata = deployment.get("metadata", {})
            spec = deployment.get("spec", {})
            namespace = str(metadata.get("namespace", "default"))
            name = str(metadata.get("name", ""))
            selector = _selector(
                spec.get("selector", {}).get("match_labels", {})
                or spec.get("selector", {}).get("matchLabels", {})
            )
            if not selector:
                continue
            pods = cluster.pods(namespace, selector)
            ready_pods = [
                pod
                for pod in pods
                if pod.get("status", {}).get("phase") == "Running"
                and any(
                    condition.get("status") == "True"
                    for condition in pod.get("status", {}).get("conditions", [])
                    if condition.get("type") == "Ready"
                )
            ]
            desired = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
            if desired < 1 or len(ready_pods) < desired:
                continue
            old_name = str(ready_pods[0]["metadata"]["name"])
            old_uid = str(ready_pods[0]["metadata"]["uid"])
            started = time.monotonic()
            cluster.delete_pod(old_name, namespace)
            replacement: dict[str, Any] | None = None
            while time.monotonic() - started < limit + 30:
                if monitor.triggered.is_set():
                    if monitor.infrastructure_error:
                        raise KubernetesError(monitor.reason or "dynamic safety monitoring failed")
                    raise UnsafeWorkloadError(
                        monitor.reason or "dynamic Pod violated local safety policy"
                    )
                for pod in cluster.pods(namespace, selector):
                    status = pod.get("status", {})
                    if pod.get("metadata", {}).get("uid") == old_uid:
                        continue
                    if status.get("phase") == "Running" and any(
                        item.get("type") == "Ready" and item.get("status") == "True"
                        for item in status.get("conditions", [])
                    ):
                        replacement = pod
                        break
                if replacement:
                    break
                time.sleep(2)
            elapsed = round(time.monotonic() - started, 3)
            sample = observe(
                "runtime.recovery",
                "recovery.pod_replacement",
                "Deployment controller Pod replacement readiness was measured.",
                resource=ResourceRef(
                    api_version="apps/v1", kind="Deployment", namespace=namespace, name=name
                ),
                data={
                    "deleted_pod_uid": old_uid,
                    "replacement_pod_uid": replacement.get("metadata", {}).get("uid")
                    if replacement
                    else None,
                    "ready_seconds": elapsed if replacement else None,
                    "deadline_seconds": limit,
                },
            )
            samples.append(sample)
            if not replacement or elapsed > limit:
                configured = profile.constraints.reliability.max_pod_replacement_ready_seconds
                findings.append(
                    Finding(
                        id=f"runtime-finding-{len(findings) + 1:05d}",
                        check_id="runtime.recovery",
                        severity=Severity.BLOCKER if configured else Severity.WARNING,
                        title=(
                            "Pod replacement readiness exceeded profile threshold"
                            if configured
                            else "Pod replacement readiness exceeded observation deadline"
                        ),
                        description=(
                            f"Deployment {namespace}/{name} did not replace a Ready Pod "
                            f"within {limit} seconds."
                        ),
                        observation_ids=(sample.id,),
                        constraint=(
                            "constraints.reliability.max_pod_replacement_ready_seconds"
                            if configured
                            else None
                        ),
                    )
                )
    except KubernetesError as exc:
        checks["runtime.recovery"] = _untested(
            "runtime.recovery", _RUNTIME_TITLES["runtime.recovery"], str(exc), failed=True
        )
        return
    own_findings = [item for item in findings if item.check_id == "runtime.recovery"]
    checks["runtime.recovery"] = CheckResult(
        id="runtime.recovery",
        title=_RUNTIME_TITLES["runtime.recovery"],
        execution_status=ExecutionStatus.COMPLETED,
        assessment=(
            Assessment.FAIL
            if any(item.severity is Severity.BLOCKER for item in own_findings)
            else Assessment.WARNING
            if own_findings
            else Assessment.NOT_APPLICABLE
            if not samples
            else Assessment.PASS
        ),
        observation_ids=tuple(item.id for item in samples),
        finding_ids=tuple(item.id for item in own_findings),
        explanation="No eligible Ready Deployment Pod was found." if not samples else None,
    )


_DNS_LOG = re.compile(
    r'(?P<ip>(?:\d{1,3}\.){3}\d{1,3}):\d+\s+-\s+\d+\s+"'
    r"(?:A|AAAA|CNAME|TXT|MX|SRV)\s+IN\s+(?P<domain>[a-zA-Z0-9_.-]+)"
)


def _observe_dns(
    cluster: ClusterReader,
    namespace: str,
    profile: CompanyProfile,
    observe: Callable[..., Observation],
    checks: dict[str, CheckResult],
    findings: list[Finding],
    artifacts: dict[str, str],
) -> None:
    try:
        logs = cluster.coredns_logs()
        artifacts["artifacts/network/coredns.log"] = logs[-500_000:]
        pod_ips = cluster.pod_ip_map()
    except KubernetesError as exc:
        checks["runtime.network"] = _untested(
            "runtime.network", _RUNTIME_TITLES["runtime.network"], str(exc), failed=True
        )
        return
    observations: list[Observation] = []
    seen: set[tuple[str, str]] = set()
    for line in logs.splitlines():
        match = _DNS_LOG.search(line)
        if not match:
            continue
        pod = pod_ips.get(match.group("ip"))
        domain = match.group("domain").rstrip(".").lower()
        if pod is None or pod[0] != namespace or (pod[2], domain) in seen:
            continue
        if domain == "localhost" or domain.endswith(
            (".localhost", ".local", ".svc", ".svc.cluster.local", ".cluster.local")
        ):
            continue
        seen.add((pod[2], domain))
        observation = observe(
            "runtime.network",
            "network.external_dns_query",
            f"Product Pod queried {domain} through CoreDNS.",
            resource=ResourceRef(api_version="v1", kind="Pod", namespace=pod[0], name=pod[1]),
            data={"pod_uid": pod[2], "domain": domain},
        )
        observations.append(observation)
        networking = profile.constraints.networking
        allowed = networking.allowed_external_domains or ()
        if networking.allow_public_egress is False and not any(
            domain == entry or domain.endswith(f".{entry}") for entry in allowed
        ):
            findings.append(
                Finding(
                    id=f"runtime-finding-{len(findings) + 1:05d}",
                    check_id="runtime.network",
                    severity=Severity.WARNING,
                    title="Observed external DNS query may conflict with no-public-egress policy",
                    description=(f"Product Pod queried {domain}; a dependency was not proven."),
                    observation_ids=(observation.id,),
                    constraint="constraints.networking.allow_public_egress",
                    limitation="A block/recovery experiment is required to prove dependency.",
                )
            )
    has_findings = any(item.check_id == "runtime.network" for item in findings)
    checks["runtime.network"] = CheckResult(
        id="runtime.network",
        title=_RUNTIME_TITLES["runtime.network"],
        execution_status=ExecutionStatus.COMPLETED,
        assessment=(
            Assessment.WARNING
            if has_findings
            else Assessment.PASS
            if observations
            else Assessment.INCONCLUSIVE
        ),
        observation_ids=tuple(item.id for item in observations),
        finding_ids=tuple(item.id for item in findings if item.check_id == "runtime.network"),
        explanation=(
            "No product external DNS query was observed in retained CoreDNS logs."
            if not observations
            else None
        ),
    )
