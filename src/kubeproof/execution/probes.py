"""Registered probes and one executor for frozen deterministic or AI plans."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kubeproof.core.domain import (
    Assessment,
    CheckResult,
    ExecutionStatus,
    Finding,
    Observation,
    ResourceRef,
    Severity,
    SourceClass,
)
from kubeproof.core.plans import (
    CpuTask,
    DnsTask,
    EvaluationPlan,
    HttpTask,
    PlanExecution,
    ProbeTask,
    RecoveryTask,
    ResourceTask,
    TaskExecution,
)
from kubeproof.core.profile import CompanyProfile
from kubeproof.execution.helm import HelmRenderRequest
from kubeproof.execution.http_service import HttpServiceExperiment
from kubeproof.execution.kubernetes import ClusterReader, KubernetesError
from kubeproof.execution.runtime import ExperimentEvidence
from kubeproof.execution.runtime_checks import (
    measure_recovery,
    measure_resources,
    observe_dns,
    untested,
)
from kubeproof.execution.runtime_monitor import MetricsCollector, SafetyMonitor, UnsafeWorkloadError


@dataclass(frozen=True)
class ProbeSpec:
    name: str
    check_id: str
    title: str
    description: str
    requires_metrics: bool = False


CATALOG = {
    spec.name: spec
    for spec in (
        ProbeSpec(
            "http_service",
            "runtime.http_service",
            "HTTP Service probe",
            "Bounded sequential GET requests to a reviewed product ClusterIP Service.",
        ),
        ProbeSpec(
            "cpu_load",
            "runtime.cpu_load",
            "CPU fixture load",
            "Bounded HTTP CPU load on the exact local CPU fixture, not generic products.",
            True,
        ),
        ProbeSpec(
            "resource_sample",
            "runtime.resources",
            "Observed resource consumption",
            "Sample product Pod CPU and memory over an explicit observation window.",
            True,
        ),
        ProbeSpec(
            "dns_observation",
            "runtime.network",
            "Observed network and DNS behavior",
            "Read retained product DNS queries; "
            "does not prove external dependency or connectivity.",
        ),
        ProbeSpec(
            "pod_recovery",
            "runtime.recovery",
            "Pod replacement readiness",
            "Delete eligible Deployment Pods and measure replacement readiness.",
        ),
    )
}


def catalog_context() -> dict[str, Any]:
    return {
        "version": "1",
        "capabilities": [
            {
                "name": spec.name,
                "description": spec.description,
                "requires_metrics": spec.requires_metrics,
            }
            for spec in CATALOG.values()
        ],
        "plan_schema": EvaluationPlan.model_json_schema(),
    }


def _cpu_request(task: CpuTask) -> Any:
    from kubeproof.intelligence.models import ConfirmedRequest, CpuGoal, RunBudget

    p = task.parameters
    return ConfirmedRequest(
        environment_id="local-kind",
        target_id="cpu-fixture",
        work_iterations=p.work_iterations,
        goal=CpuGoal(
            target_rps=p.min_success_rps,
            max_p95_ms=p.max_p95_ms,
            max_failed_requests=p.max_failed_requests,
            max_cpu_millicores=p.max_cpu_millicores,
        ),
        budget=RunBudget(
            max_plan_versions=1,
            max_elapsed_seconds=180,
            max_model_calls=1,
            max_tool_calls=1,
            max_requests_per_trial=p.requests,
        ),
    )


def _remap(evidence: ExperimentEvidence, task: ProbeTask, check_id: str) -> ExperimentEvidence:
    observations = {item.id: f"probe-{task.id}-{item.id}" for item in evidence.observations}
    findings = {item.id: f"probe-{task.id}-{item.id}" for item in evidence.findings}
    paths = {
        path: f"artifacts/probes/{task.id}/{path.removeprefix('artifacts/')}"
        for path in evidence.artifacts
    }
    remapped_observations = []
    for item in evidence.observations:
        data = dict(item.data)
        data["plan_task_id"] = task.id
        data["artifact_refs"] = list(paths.values())
        if isinstance(data.get("measurement"), dict):
            measurement = dict(data["measurement"])
            if measurement.get("evidence_ref") in paths:
                measurement["evidence_ref"] = paths[measurement["evidence_ref"]]
            data["measurement"] = measurement
        remapped_observations.append(
            item.model_copy(
                update={"id": observations[item.id], "check_id": check_id, "data": data}
            )
        )
    return ExperimentEvidence(
        tuple(
            item.model_copy(
                update={
                    "id": check_id,
                    "observation_ids": tuple(observations[key] for key in item.observation_ids),
                    "finding_ids": tuple(findings[key] for key in item.finding_ids),
                }
            )
            for item in evidence.checks
        ),
        tuple(remapped_observations),
        tuple(
            item.model_copy(
                update={
                    "id": findings[item.id],
                    "check_id": check_id,
                    "observation_ids": tuple(observations[key] for key in item.observation_ids),
                }
            )
            for item in evidence.findings
        ),
        {paths[path]: value for path, value in evidence.artifacts.items()},
    )


class PlannedExperiment:
    """One plan snapshot; task outcomes select existing tasks but never revise it."""

    check_id = "runtime.plan"
    title = "Frozen probe plan"
    requires_metrics = False
    owns_runtime_checks = True

    def __init__(self, plan: EvaluationPlan, profile: CompanyProfile):
        self.plan = plan
        self.profile = profile
        self.plan_sha256 = plan.digest()
        self._http = {
            task.id: HttpServiceExperiment(task.parameters)
            for task in plan.tasks
            if isinstance(task, HttpTask)
        }
        self.monitor: SafetyMonitor | None = None
        self.deployments: list[dict[str, Any]] = []
        self.metrics_error: str | None = None
        self.dns_error: str | None = None
        self.execution: PlanExecution | None = None

    def check_for(self, task: ProbeTask) -> str:
        first = next(item for item in self.plan.tasks if item.capability == task.capability)
        base = CATALOG[task.capability].check_id
        return base if first.id == task.id else f"{base}.{task.id}"

    def validate_resources(
        self, resources: tuple[dict[str, Any], ...], request: HelmRenderRequest
    ) -> None:
        for task in self.plan.tasks:
            if isinstance(task, HttpTask):
                self._http[task.id].validate_resources(resources, request)
            elif isinstance(task, CpuTask):
                from kubeproof.execution.cpu_fixture import validate_fixture_resources

                validate_fixture_resources(resources, request, _cpu_request(task))

    def prepare(self, cluster_name: str, kubeconfig: Path) -> None:
        if self._http:
            first = next(iter(self._http.values()))
            first.prepare(cluster_name, kubeconfig)
            for probe in self._http.values():
                probe.image_id = first.image_id
        if any(isinstance(task, CpuTask) for task in self.plan.tasks):
            from kubeproof.execution.cpu_worker import prepare_fixture_image

            prepare_fixture_image(cluster_name)

    def configure_runtime(
        self,
        *,
        monitor: SafetyMonitor,
        deployments: list[dict[str, Any]],
        metrics_error: str | None,
        dns_error: str | None,
    ) -> None:
        self.monitor, self.deployments = monitor, deployments
        self.metrics_error, self.dns_error = metrics_error, dns_error

    def not_run(
        self, reason: str, *, failed: bool = False, cancelled: bool = False
    ) -> ExperimentEvidence:
        self.execution = PlanExecution(
            plan_sha256=self.plan_sha256,
            tasks=tuple(
                TaskExecution(
                    task_id=task.id,
                    state="cancelled"
                    if cancelled
                    else "infrastructure_failure"
                    if failed
                    else "not_tested",
                    check_ids=(self.check_for(task),),
                    explanation=reason,
                )
                for task in self.plan.tasks
            ),
        )
        return self._finish(
            tuple(
                untested(
                    self.check_for(task),
                    CATALOG[task.capability].title,
                    reason,
                    failed=failed,
                    cancelled=cancelled,
                )
                for task in self.plan.tasks
            ),
            (),
            (),
            {},
        )

    def _finish(
        self,
        checks: tuple[CheckResult, ...],
        observations: tuple[Observation, ...],
        findings: tuple[Finding, ...],
        artifacts: dict[str, str],
    ) -> ExperimentEvidence:
        artifacts["artifacts/plan/plan.json"] = self.plan.model_dump_json(indent=2) + "\n"
        if self.execution:
            artifacts["artifacts/plan/execution.json"] = (
                self.execution.model_dump_json(indent=2) + "\n"
            )
        return ExperimentEvidence(checks, observations, findings, artifacts)

    def _perform(
        self,
        task: ProbeTask,
        cluster: ClusterReader,
        kubeconfig: Path,
        namespace: str,
        cancel_if: Callable[[], bool],
    ) -> ExperimentEvidence:
        if isinstance(task, HttpTask):
            return self._http[task.id].run(cluster, kubeconfig, namespace, cancel_if)
        if isinstance(task, CpuTask):
            from kubeproof.execution.cpu_worker import CpuLoadWorker
            from kubeproof.execution.errors import TrialInfrastructureError
            from kubeproof.intelligence.models import PlanTask, TrialStatus

            worker = CpuLoadWorker(cluster, kubeconfig, namespace, cancel_if)
            try:
                record = worker.execute(
                    _cpu_request(task),
                    PlanTask(
                        id=task.id,
                        capability="cpu_load",
                        parameters={
                            "offered_rps": task.parameters.offered_rps,
                            "requests": task.parameters.requests,
                        },
                        rationale=task.rationale,
                    ),
                    1,
                )
            except TrialInfrastructureError as exc:
                return ExperimentEvidence(
                    (untested("runtime.cpu_load", "CPU fixture load", str(exc), failed=True),),
                    (),
                    (),
                    worker.artifacts,
                )
            if record.status is not TrialStatus.COMPLETED or record.observation is None:
                raise KubernetesError("CPU worker did not return complete measurements")
            p, m = task.parameters, record.observation
            passed = (
                m["achieved_rps"] >= p.min_success_rps
                and m["p95_ms"] is not None
                and m["p95_ms"] <= p.max_p95_ms
                and m["failed_requests"] <= p.max_failed_requests
                and m["cpu_millicores"] <= p.max_cpu_millicores
            )
            pod = worker.pod_names.get((task.id, 1))
            observation = Observation(
                id="cpu-observation",
                check_id="runtime.cpu_load",
                source_class=SourceClass.RUNTIME,
                observation_type="cpu.bounded_load_trial",
                summary=record.explanation,
                resource=ResourceRef(api_version="v1", kind="Pod", namespace=namespace, name=pod)
                if pod
                else None,
                data={
                    "measurement": m,
                    "parameters": p.model_dump(mode="json"),
                    "meets_goal": passed,
                },
            )
            findings = (
                ()
                if passed
                else (
                    Finding(
                        id="cpu-finding",
                        check_id="runtime.cpu_load",
                        severity=Severity.BLOCKER,
                        title="CPU trial did not meet confirmed requirements",
                        description=(
                            "Measured throughput, latency, errors or sampled CPU "
                            "did not meet this task's thresholds."
                        ),
                        observation_ids=(observation.id,),
                    ),
                )
            )
            return ExperimentEvidence(
                (
                    CheckResult(
                        id="runtime.cpu_load",
                        title="CPU fixture load",
                        execution_status=ExecutionStatus.COMPLETED,
                        assessment=Assessment.PASS if passed else Assessment.FAIL,
                        observation_ids=(observation.id,),
                        finding_ids=tuple(item.id for item in findings),
                    ),
                ),
                (observation,),
                findings,
                worker.artifacts,
            )
        checks: dict[str, CheckResult] = {}
        observations: list[Observation] = []
        findings_list: list[Finding] = []
        artifacts: dict[str, str] = {}

        def observe(check_id: str, kind: str, summary: str, **kwargs: Any) -> Observation:
            item = Observation(
                id=f"obs-{len(observations) + 1:05d}",
                check_id=check_id,
                source_class=SourceClass.RUNTIME,
                observation_type=kind,
                summary=summary,
                **kwargs,
            )
            observations.append(item)
            return item

        if isinstance(task, ResourceTask):
            started = time.monotonic()
            collector = MetricsCollector(cluster, namespace)
            collector.start()
            try:
                end = started + task.parameters.duration_seconds
                while time.monotonic() < end:
                    if cancel_if():
                        raise UnsafeWorkloadError("resource observation cancelled")
                    time.sleep(min(0.1, max(0, end - time.monotonic())))
            finally:
                collector.stop()
            artifacts["artifacts/resources/snapshots.json"] = (
                json.dumps(
                    [
                        {
                            "captured_at_monotonic": snapshot.captured_at,
                            "pods": [
                                {
                                    "name": pod.get("metadata", {}).get("name"),
                                    "uid": pod.get("metadata", {}).get("uid"),
                                    "containers_expected": [
                                        container.get("name")
                                        for container in pod.get("spec", {}).get("containers", [])
                                    ],
                                }
                                for pod in snapshot.pods
                            ],
                            "metrics": snapshot.metrics,
                        }
                        for snapshot in collector.snapshots
                    ],
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            if collector.error:
                raise KubernetesError(collector.error)
            measure_resources(
                collector.snapshots,
                namespace,
                self.profile,
                started,
                observe,
                checks,
                findings_list,
            )
        elif isinstance(task, DnsTask):
            if self.dns_error:
                raise KubernetesError(self.dns_error)
            observe_dns(cluster, namespace, self.profile, observe, checks, findings_list, artifacts)
        elif isinstance(task, RecoveryTask):
            # The proxy combines safety cancellation and the plan wall-clock budget.
            class Signal:
                def is_set(self) -> bool:
                    return cancel_if()

            class Monitor:
                triggered = Signal()
                infrastructure_error = False
                reason = "recovery cancelled by safety or plan budget"

            measure_recovery(
                cluster,
                self.deployments,
                self.profile,
                task.parameters.max_targets,
                observe,
                checks,
                findings_list,
                Monitor(),  # type: ignore[arg-type]
            )
        return ExperimentEvidence(
            tuple(checks.values()), tuple(observations), tuple(findings_list), artifacts
        )

    @staticmethod
    def _claims(task: ProbeTask) -> frozenset[str]:
        if isinstance(task, RecoveryTask):
            return frozenset({"exclusive-product"})
        if isinstance(task, CpuTask):
            return frozenset({"cpu-load"})
        if isinstance(task, HttpTask):
            return frozenset({f"http:{task.parameters.service}"})
        return frozenset()

    def run(
        self,
        cluster: ClusterReader,
        kubeconfig: Path,
        namespace: str,
        cancel_if: Callable[[], bool],
    ) -> ExperimentEvidence:
        started = time.monotonic()
        cancelled = threading.Event()
        results: dict[str, ExperimentEvidence] = {}
        executions: dict[str, TaskExecution] = {}

        def stop() -> bool:
            return (
                cancelled.is_set()
                or cancel_if()
                or time.monotonic() - started >= self.plan.budget.max_elapsed_seconds
            )

        def run_task(task: ProbeTask) -> tuple[ExperimentEvidence, TaskExecution]:
            begin = datetime.now(UTC).isoformat()
            try:
                if stop():
                    raise UnsafeWorkloadError("safety cancellation or plan time budget exhausted")
                if CATALOG[task.capability].requires_metrics and self.metrics_error:
                    raise KubernetesError(self.metrics_error)
                result = self._perform(task, cluster, kubeconfig, namespace, stop)
                result = _remap(result, task, self.check_for(task))
            except UnsafeWorkloadError as exc:
                cancelled.set()
                infrastructure_error = bool(self.monitor and self.monitor.infrastructure_error)
                reason = self.monitor.reason if infrastructure_error and self.monitor else str(exc)
                result = ExperimentEvidence(
                    (
                        untested(
                            self.check_for(task),
                            CATALOG[task.capability].title,
                            reason or str(exc),
                            failed=infrastructure_error,
                            cancelled=not infrastructure_error,
                        ),
                    ),
                    (),
                    (),
                    {},
                )
            except (KubernetesError, OSError, ValueError) as exc:
                result = ExperimentEvidence(
                    (
                        untested(
                            self.check_for(task),
                            CATALOG[task.capability].title,
                            str(exc),
                            failed=True,
                        ),
                    ),
                    (),
                    (),
                    {},
                )
            status = next(
                (
                    check.execution_status
                    for check in result.checks
                    if check.execution_status is not ExecutionStatus.COMPLETED
                ),
                ExecutionStatus.COMPLETED,
            )
            execution = TaskExecution(
                task_id=task.id,
                state=status.value,
                check_ids=tuple(check.id for check in result.checks),
                explanation=next(
                    (check.explanation for check in result.checks if check.explanation), None
                ),
                started_at=begin,
                completed_at=datetime.now(UTC).isoformat(),
            )
            return result, execution

        with ThreadPoolExecutor(
            max_workers=self.plan.budget.max_parallel_tasks, thread_name_prefix="kubeproof-probe"
        ) as pool:
            while len(executions) < len(self.plan.tasks):
                ready: list[ProbeTask] = []
                claims: set[str] = set()
                for task in self.plan.tasks:
                    if task.id in executions or any(
                        dep not in executions for dep in task.depends_on
                    ):
                        continue
                    dependencies = [
                        check for dep in task.depends_on for check in results[dep].checks
                    ]
                    eligible = all(
                        check.execution_status is ExecutionStatus.COMPLETED
                        and check.assessment is not Assessment.NOT_TESTED
                        for check in dependencies
                    )
                    if task.when == "passed":
                        eligible = eligible and all(
                            check.assessment is Assessment.PASS for check in dependencies
                        )
                    elif task.when == "failed":
                        eligible = eligible and any(
                            check.assessment is Assessment.FAIL for check in dependencies
                        )
                    reason = (
                        "Safety cancellation or plan time budget exhausted."
                        if stop()
                        else "Dependency condition was not satisfied."
                        if not eligible
                        else None
                    )
                    if reason:
                        infrastructure_error = bool(
                            stop() and self.monitor and self.monitor.infrastructure_error
                        )
                        if infrastructure_error and self.monitor:
                            reason = self.monitor.reason or "Runtime monitoring failed."
                        result = ExperimentEvidence(
                            (
                                untested(
                                    self.check_for(task),
                                    CATALOG[task.capability].title,
                                    reason,
                                    failed=infrastructure_error,
                                    cancelled=stop() and not infrastructure_error,
                                ),
                            ),
                            (),
                            (),
                            {},
                        )
                        results[task.id] = result
                        executions[task.id] = TaskExecution(
                            task_id=task.id,
                            state="infrastructure_failure"
                            if infrastructure_error
                            else "cancelled"
                            if stop()
                            else "skipped",
                            check_ids=(self.check_for(task),),
                            explanation=reason,
                        )
                        continue
                    own = self._claims(task)
                    if ready and (
                        "exclusive-product" in claims or "exclusive-product" in own or own & claims
                    ):
                        continue
                    ready.append(task)
                    claims.update(own)
                    if len(ready) >= self.plan.budget.max_parallel_tasks:
                        break
                futures = [(task, pool.submit(run_task, task)) for task in ready]
                for task, future in futures:
                    results[task.id], executions[task.id] = future.result()
        if self.plan.digest() != self.plan_sha256:
            raise KubernetesError("frozen plan changed during execution")
        self.execution = PlanExecution(
            plan_sha256=self.plan_sha256,
            tasks=tuple(executions[task.id] for task in self.plan.tasks),
        )
        checks = tuple(check for task in self.plan.tasks for check in results[task.id].checks)
        observations = tuple(
            item for task in self.plan.tasks for item in results[task.id].observations
        )
        findings = tuple(item for task in self.plan.tasks for item in results[task.id].findings)
        artifacts = {
            path: value
            for task in self.plan.tasks
            for path, value in results[task.id].artifacts.items()
        }
        return self._finish(checks, observations, findings, artifacts)
