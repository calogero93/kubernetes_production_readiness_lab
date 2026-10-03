"""Live CPU admission is narrow even though the upload accepts Helm archives."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from kubeproof.core.domain import Assessment, ExecutionStatus
from kubeproof.core.profile import CompanyProfile
from kubeproof.execution.cpu_fixture import validate_fixture_resources
from kubeproof.execution.cpu_live import CpuLiveExperiment, _CpuRun, _report, _run_pilot
from kubeproof.execution.cpu_worker import CpuLoadWorker
from kubeproof.execution.helm import HelmRenderRequest
from kubeproof.execution.kubernetes import ClusterReader
from kubeproof.intelligence.capabilities import cpu_capabilities
from kubeproof.intelligence.control import PlanRejected, validate_plan
from kubeproof.intelligence.models import (
    ConfirmedRequest,
    CpuGoal,
    CpuMeasurement,
    PlanTask,
    RunBudget,
    RunOutcome,
    TestPlan,
    TrialRecord,
    TrialStatus,
)
from kubeproof.intelligence.workflow import TrialInfrastructureError


def request() -> ConfirmedRequest:
    return ConfirmedRequest(
        environment_id="local-kind",
        target_id="cpu-fixture",
        work_iterations=100_000,
        goal=CpuGoal(
            target_rps=50,
            max_p95_ms=100,
            max_failed_requests=0,
            max_cpu_millicores=500,
        ),
        budget=RunBudget(
            max_plan_versions=1,
            max_elapsed_seconds=900,
            max_model_calls=1,
            max_tool_calls=3,
            max_requests_per_trial=10_000,
        ),
    )


def fixture_resources() -> tuple[dict[str, object], ...]:
    labels = {
        "app.kubernetes.io/name": "cpu-fixture",
        "app.kubernetes.io/instance": "kubeproof-target",
    }
    return (
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "kubeproof-target",
                "namespace": "kubeproof-product",
                "labels": labels,
            },
            "spec": {
                "replicas": 2,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 65532,
                            "runAsGroup": 65532,
                        },
                        "containers": [
                            {
                                "name": "cpu-fixture",
                                "image": "kubeproof-cpu-fixture:local",
                                "imagePullPolicy": "IfNotPresent",
                                "args": ["--host", "0.0.0.0", "--iterations", "100000"],
                                "ports": [{"name": "http", "containerPort": 8080}],
                                "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}},
                                "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}},
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "32Mi"},
                                    "limits": {"cpu": "500m", "memory": "128Mi"},
                                },
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "kubeproof-target",
                "namespace": "kubeproof-product",
                "labels": labels,
            },
            "spec": {
                "type": "ClusterIP",
                "selector": labels,
                "ports": [{"name": "http", "port": 8080, "targetPort": "http"}],
            },
        },
    )


def test_fixture_contract_accepts_only_selected_image_and_service() -> None:
    resources = fixture_resources()
    validate_fixture_resources(resources, HelmRenderRequest(chart="fixture.tgz"), request())
    deployment = resources[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]  # type: ignore[index]
    container["image"] = "remote/unreviewed:latest"
    with pytest.raises(ValueError, match="local CPU image"):
        validate_fixture_resources(resources, HelmRenderRequest(chart="fixture.tgz"), request())


def test_cpu_baseline_preserves_the_confirmed_tool_call_budget(
    strict_profile: CompanyProfile,
) -> None:
    confirmed = request()
    plan = CpuLiveExperiment(confirmed).baseline_plan(
        fixture_resources(),
        HelmRenderRequest(chart="fixture.tgz"),
        strict_profile,
    )
    assert plan.budget.max_tasks == confirmed.budget.max_tool_calls
    assert len(plan.tasks) == 1
    assert plan.origin == "deterministic"


def test_fixture_contract_rejects_replica_explosion_and_external_service() -> None:
    resources = fixture_resources()
    deployment = resources[0]["spec"]  # type: ignore[index]
    deployment["replicas"] = 100
    with pytest.raises(ValueError, match="between one and three replicas"):
        validate_fixture_resources(resources, HelmRenderRequest(chart="fixture.tgz"), request())
    deployment["replicas"] = 2
    service = resources[1]["spec"]  # type: ignore[index]
    service["externalIPs"] = ["127.0.0.1"]
    with pytest.raises(ValueError, match="Service differs"):
        validate_fixture_resources(resources, HelmRenderRequest(chart="fixture.tgz"), request())


def test_live_catalog_rejects_unmeasurably_short_cpu_trials() -> None:
    plan = TestPlan(
        version=1,
        tasks=(
            PlanTask(
                id="too-short",
                capability="cpu_load",
                parameters={"offered_rps": 50, "requests": 10},
                rationale="Try a very brief trial.",
            ),
        ),
    )
    with pytest.raises(PlanRejected, match="20 and 90 seconds"):
        validate_plan(request(), plan, catalog=cpu_capabilities(live=True))


def test_report_keeps_negative_pilot_distinct_from_infrastructure_failure() -> None:
    confirmed = request()
    plan = TestPlan(
        version=1,
        tasks=(
            PlanTask(
                id="cpu-pilot",
                capability="cpu_load",
                parameters={"offered_rps": 50, "requests": 1500},
                rationale="One bounded baseline.",
            ),
        ),
    )
    worker = CpuLoadWorker(cast(ClusterReader, object()), Path("/unused"), "sandbox", lambda: False)
    worker.pod_names[("cpu-pilot", 1)] = "fixture-pod"
    negative = TrialRecord(
        task_id="cpu-pilot",
        attempt=1,
        status=TrialStatus.COMPLETED,
        observation=CpuMeasurement(
            achieved_rps=40,
            p95_ms=80,
            failed_requests=0,
            cpu_millicores=400,
            cpu_sample_complete=True,
            evidence_ref="artifacts/cpu/cpu-pilot-attempt-1.json",
        ).model_dump(mode="json"),
        explanation="Measured one Pod.",
    )
    run = _CpuRun(plan, (negative,), RunOutcome.INCONCLUSIVE, "Pilot goal not met.")
    evidence = _report(confirmed, cpu_capabilities(live=True), worker, "sandbox", run)
    assert evidence.checks[0].execution_status is ExecutionStatus.COMPLETED
    assert evidence.checks[0].assessment is Assessment.INCONCLUSIVE
    assert evidence.checks[0].explanation == "Pilot goal not met."
    assert evidence.observations[0].data["meets_goal"] is False
    assert evidence.observations[0].resource is not None
    assert evidence.observations[0].resource.name == "fixture-pod"
    assert evidence.findings == ()

    failed = TrialRecord(
        task_id="cpu-pilot",
        attempt=1,
        status=TrialStatus.INFRASTRUCTURE_FAILURE,
        explanation="Metrics API unavailable.",
    )
    run = _CpuRun(plan, (failed,), RunOutcome.BLOCKED, "No valid measurement.")
    evidence = _report(confirmed, cpu_capabilities(live=True), worker, "sandbox", run)
    assert evidence.checks[0].execution_status is ExecutionStatus.INFRASTRUCTURE_FAILURE
    assert evidence.observations[0].data["measurement"] is None


def test_pilot_retries_infrastructure_then_reports_single_negative_trial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = CpuLoadWorker(cast(ClusterReader, object()), Path("/unused"), "sandbox", lambda: False)
    calls: list[int] = []

    def execute(confirmed: ConfirmedRequest, task: PlanTask, attempt: int) -> TrialRecord:
        del confirmed
        calls.append(attempt)
        if attempt == 1:
            raise TrialInfrastructureError("Metrics API unavailable")
        return TrialRecord(
            task_id=task.id,
            attempt=attempt,
            status=TrialStatus.COMPLETED,
            observation=CpuMeasurement(
                achieved_rps=40,
                p95_ms=80,
                failed_requests=0,
                cpu_millicores=400,
                cpu_sample_complete=True,
                evidence_ref="artifact:cpu-pilot",
            ).model_dump(mode="json"),
            explanation="Measured one Pod.",
        )

    monkeypatch.setattr(worker, "execute", execute)
    run = _run_pilot(request(), cpu_capabilities(live=True), worker)
    assert calls == [1, 2]
    assert [record.status for record in run.records] == [
        TrialStatus.INFRASTRUCTURE_FAILURE,
        TrialStatus.COMPLETED,
    ]
    assert run.outcome is RunOutcome.INCONCLUSIVE
    assert "single trial" in run.explanation
