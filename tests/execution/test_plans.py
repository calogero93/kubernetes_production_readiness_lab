"""A frozen composition must retain evidence across branches and concurrent probes."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from kubeproof.core.domain import Assessment, CheckResult, ExecutionStatus, HttpProbeOptions
from kubeproof.core.plans import (
    DnsTask,
    EvaluationPlan,
    HttpTask,
    PlanBudget,
    RecoveryTask,
    ResourceTask,
)
from kubeproof.core.profile import CompanyProfile
from kubeproof.execution.kubernetes import KubernetesError
from kubeproof.execution.probes import PlannedExperiment
from kubeproof.execution.runtime import ExperimentEvidence


def plan(*tasks: Any, parallel: int = 1, origin: str = "user") -> EvaluationPlan:
    return EvaluationPlan(
        objective="Exercise probe composition.",
        origin=origin,
        budget=PlanBudget(max_parallel_tasks=parallel),
        tasks=tasks,
    )


def evidence(assessment: Assessment = Assessment.PASS) -> ExperimentEvidence:
    return ExperimentEvidence(
        (
            CheckResult(
                id="runtime.resources",
                title="probe",
                execution_status=ExecutionStatus.COMPLETED,
                assessment=assessment,
            ),
        ),
        (),
        (),
        {},
    )


def test_plan_rejects_unsafe_or_ambiguous_graphs() -> None:
    first = ResourceTask(id="first", rationale="baseline")
    for tasks in (
        (first, first),
        (ResourceTask(id="second", rationale="missing", depends_on=("absent",)),),
        (
            first.model_copy(update={"depends_on": ("second",)}),
            ResourceTask(id="second", rationale="cycle", depends_on=("first",)),
        ),
        (first.model_copy(update={"when": "failed"}),),
    ):
        with pytest.raises(ValidationError):
            plan(*tasks)
    with pytest.raises(ValidationError):
        EvaluationPlan.model_validate(
            {
                "objective": "unknown probe",
                "tasks": [
                    {
                        "id": "shell",
                        "capability": "shell",
                        "rationale": "arbitrary execution",
                        "parameters": {"command": "anything"},
                    }
                ],
            }
        )


def test_plan_and_nested_parameters_are_immutable() -> None:
    frozen = plan(
        HttpTask(id="http", rationale="verify", parameters=HttpProbeOptions(service="api"))
    )
    before = frozen.digest()
    with pytest.raises(ValidationError):
        frozen.tasks[0].parameters.requests = 30
    with pytest.raises(ValidationError):
        frozen.budget.max_parallel_tasks = 4
    assert frozen.digest() == before


def test_conditions_select_existing_tasks_without_replanning(
    strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = plan(
        ResourceTask(id="baseline", rationale="measure"),
        DnsTask(id="on-pass", rationale="follow success", depends_on=("baseline",), when="passed"),
        DnsTask(id="on-fail", rationale="follow failure", depends_on=("baseline",), when="failed"),
        DnsTask(id="independent", rationale="always inspect"),
    )
    experiment = PlannedExperiment(frozen, strict_profile)
    executed: list[str] = []

    def perform(task: Any, *_args: Any) -> ExperimentEvidence:
        executed.append(task.id)
        return evidence(Assessment.FAIL if task.id == "baseline" else Assessment.PASS)

    monkeypatch.setattr(experiment, "_perform", perform)
    result = experiment.run(None, Path("config"), "product", lambda: False)  # type: ignore[arg-type]
    assert executed == ["baseline", "on-fail", "independent"]
    assert experiment.plan.digest() == frozen.digest()
    assert experiment.execution is not None
    assert [task.state for task in experiment.execution.tasks] == [
        "completed",
        "skipped",
        "completed",
        "completed",
    ]
    assert len({check.id for check in result.checks}) == 4
    assert not result.findings


def test_infrastructure_failure_blocks_dependents_but_not_independent_probes(
    strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = PlannedExperiment(
        plan(
            ResourceTask(id="first", rationale="measure"),
            DnsTask(id="dependent", rationale="needs measurements", depends_on=("first",)),
            DnsTask(id="independent", rationale="different question"),
        ),
        strict_profile,
    )

    def perform(task: Any, *_args: Any) -> ExperimentEvidence:
        if task.id == "first":
            raise KubernetesError("metrics unavailable")
        return evidence()

    monkeypatch.setattr(experiment, "_perform", perform)
    result = experiment.run(None, Path("config"), "product", lambda: False)  # type: ignore[arg-type]
    assert experiment.execution is not None
    assert [task.state for task in experiment.execution.tasks] == [
        "infrastructure_failure",
        "skipped",
        "completed",
    ]
    assert not result.findings


@pytest.mark.parametrize("origin", ["deterministic", "ai"])
def test_both_plan_origins_use_the_same_concurrent_executor(
    origin: str, strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = PlannedExperiment(
        plan(
            ResourceTask(id="resources", rationale="during HTTP"),
            HttpTask(
                id="http", rationale="during sampling", parameters=HttpProbeOptions(service="api")
            ),
            parallel=2,
            origin=origin,
        ),
        strict_profile,
    )
    rendezvous = threading.Barrier(2)

    def perform(*_args: Any) -> ExperimentEvidence:
        rendezvous.wait(timeout=2)
        return evidence()

    monkeypatch.setattr(experiment, "_perform", perform)
    result = experiment.run(None, Path("config"), "product", lambda: False)  # type: ignore[arg-type]
    assert all(check.assessment is Assessment.PASS for check in result.checks)
    assert "artifacts/plan/plan.json" in result.artifacts
    assert "artifacts/plan/execution.json" in result.artifacts


def test_destructive_recovery_does_not_overlap_other_probes(
    strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = PlannedExperiment(
        plan(
            RecoveryTask(id="recovery", rationale="delete Pod"),
            ResourceTask(id="resources", rationale="sample"),
            parallel=2,
        ),
        strict_profile,
    )
    lock = threading.Lock()

    def perform(*_args: Any) -> ExperimentEvidence:
        assert lock.acquire(blocking=False)
        threading.Event().wait(0.02)
        lock.release()
        return evidence()

    monkeypatch.setattr(experiment, "_perform", perform)
    result = experiment.run(None, Path("config"), "product", lambda: False)  # type: ignore[arg-type]
    assert all(check.assessment is Assessment.PASS for check in result.checks)


def test_cancellation_accounts_for_every_task(strict_profile: CompanyProfile) -> None:
    experiment = PlannedExperiment(
        plan(
            ResourceTask(id="a", rationale="first"),
            DnsTask(id="b", rationale="after", depends_on=("a",)),
        ),
        strict_profile,
    )
    result = experiment.run(None, Path("config"), "product", lambda: True)  # type: ignore[arg-type]
    assert len(result.checks) == 2
    assert all(check.execution_status is ExecutionStatus.CANCELLED for check in result.checks)
    assert experiment.execution is not None
    assert all(task.state == "cancelled" for task in experiment.execution.tasks)


def test_time_budget_cancels_predefined_tasks_without_starting_them(
    strict_profile: CompanyProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = EvaluationPlan(
        objective="Bounded evaluation",
        budget=PlanBudget(max_elapsed_seconds=1),
        tasks=(DnsTask(id="dns", rationale="inspect"),),
    )
    experiment = PlannedExperiment(frozen, strict_profile)
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr("kubeproof.execution.probes.time.monotonic", lambda: next(ticks, 2.0))
    monkeypatch.setattr(
        experiment, "_perform", lambda *_: pytest.fail("expired tasks must not run")
    )
    result = experiment.run(None, Path("config"), "product", lambda: False)  # type: ignore[arg-type]
    assert result.checks[0].execution_status is ExecutionStatus.CANCELLED
    assert not result.observations


def test_monitor_failure_is_infrastructure_uncertainty_not_safety_cancellation(
    strict_profile: CompanyProfile,
) -> None:
    experiment = PlannedExperiment(plan(DnsTask(id="dns", rationale="inspect")), strict_profile)
    experiment.monitor = SimpleNamespace(
        infrastructure_error=True, reason="Cluster API is unavailable."
    )
    result = experiment.run(None, Path("config"), "product", lambda: True)  # type: ignore[arg-type]
    assert result.checks[0].execution_status is ExecutionStatus.INFRASTRUCTURE_FAILURE
    assert result.checks[0].assessment is Assessment.NOT_TESTED
    assert not result.findings
    assert experiment.execution is not None
    assert experiment.execution.tasks[0].state == "infrastructure_failure"
