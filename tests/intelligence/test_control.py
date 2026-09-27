"""The model can propose work, but these rules alone admit and assess it."""

import pytest
from pydantic import Field

from kubeproof.intelligence.capabilities import CapabilityCatalog, CapabilitySpec, cpu_capabilities
from kubeproof.intelligence.control import (
    PlanRejected,
    blocked_task_ids,
    outcome,
    ready_tasks,
    validate_plan,
)
from kubeproof.intelligence.models import (
    ConfirmedRequest,
    CpuGoal,
    CpuMeasurement,
    PlanTask,
    RunBudget,
    RunOutcome,
    StrictModel,
    TestPlan,
    TrialRecord,
    TrialStatus,
)


def request() -> ConfirmedRequest:
    return ConfirmedRequest(
        environment_id="sandbox-1",
        target_id="cpu-fixture",
        work_iterations=100_000,
        goal=CpuGoal(
            target_rps=50,
            max_p95_ms=100,
            max_failed_requests=0,
            max_cpu_millicores=500,
        ),
        budget=RunBudget(
            max_plan_versions=5,
            max_elapsed_seconds=300,
            max_model_calls=5,
            max_tool_calls=15,
            max_requests_per_trial=500,
        ),
    )


def task(task_id: str, rate: float, *, depends_on: tuple[str, ...] = ()) -> PlanTask:
    return PlanTask(
        id=task_id,
        capability="cpu_load",
        parameters={"offered_rps": rate, "requests": 100},
        depends_on=depends_on,
        rationale="Measure the CPU-bound fixture at this fixed rate.",
    )


class ProbeParameters(StrictModel):
    endpoint: str = Field(min_length=1)


class ProbeObservation(StrictModel):
    reachable: bool
    evidence_ref: str = Field(min_length=1)


def mixed_catalog(*, conflict: bool = False) -> CapabilityCatalog:
    probe = CapabilitySpec(
        name="fake_probe",
        description="Test-only endpoint probe.",
        parameters_model=ProbeParameters,
        observation_model=ProbeObservation,
        validate_parameters=lambda request, parameters: None,
        validate_observation=lambda observation: None,
        resource_keys=lambda request, parameters: frozenset(
            {f"{request.environment_id}:{request.target_id}:load" if conflict else "probe"}
        ),
    )
    return CapabilityCatalog((cpu_capabilities().get("cpu_load"), probe))


def probe_task(*, depends_on: tuple[str, ...] = ()) -> PlanTask:
    return PlanTask(
        id="probe",
        capability="fake_probe",
        parameters={"endpoint": "fixture.local"},
        depends_on=depends_on,
        rationale="Check endpoint before evaluating load.",
    )


def completed(task_id: str, *, rate: float = 50, cpu: int = 400) -> TrialRecord:
    return TrialRecord(
        task_id=task_id,
        attempt=1,
        status=TrialStatus.COMPLETED,
        observation=CpuMeasurement(
            achieved_rps=rate,
            p95_ms=80,
            failed_requests=0,
            cpu_millicores=cpu,
            cpu_sample_complete=True,
            evidence_ref=f"artifact:{task_id}",
        ).model_dump(mode="json"),
        explanation="valid measured trial",
    )


def infra(task_id: str, attempt: int) -> TrialRecord:
    return TrialRecord(
        task_id=task_id,
        attempt=attempt,
        status=TrialStatus.INFRASTRUCTURE_FAILURE,
        explanation="metrics API unavailable before load",
    )


def test_plan_rejects_cycles_and_request_overrun() -> None:
    with pytest.raises(PlanRejected, match="cycle"):
        validate_plan(
            request(),
            TestPlan(
                version=1,
                tasks=(task("a", 20, depends_on=("b",)), task("b", 30, depends_on=("a",))),
            ),
        )
    with pytest.raises(PlanRejected, match="request budget"):
        validate_plan(
            request(),
            TestPlan(
                version=1,
                tasks=(
                    PlanTask(
                        id="large",
                        capability="cpu_load",
                        parameters={"offered_rps": 50, "requests": 501},
                        rationale="Try a longer sample.",
                    ),
                ),
            ),
        )


def test_plan_rejects_unregistered_capability_and_bad_typed_parameters() -> None:
    unknown = task("unknown", 20).model_copy(update={"capability": "unregistered"})
    with pytest.raises(PlanRejected, match="unregistered capability"):
        validate_plan(request(), TestPlan(version=1, tasks=(unknown,)))
    malformed = task("malformed", 20).model_copy(update={"parameters": {"requests": 100}})
    with pytest.raises(PlanRejected, match="invalid parameters"):
        validate_plan(request(), TestPlan(version=1, tasks=(malformed,)))


def test_mixed_plan_uses_deterministic_resource_conflicts() -> None:
    plan = TestPlan(version=1, tasks=(task("cpu", 50), probe_task()))
    independent = mixed_catalog()
    validate_plan(request(), plan, catalog=independent)
    assert {item.id for item in ready_tasks(request(), plan, (), catalog=independent)} == {
        "cpu",
        "probe",
    }
    conflicting = mixed_catalog(conflict=True)
    validate_plan(request(), plan, catalog=conflicting)
    assert tuple(item.id for item in ready_tasks(request(), plan, (), catalog=conflicting)) == (
        "cpu",
    )


def test_revision_cannot_rewrite_prior_trial() -> None:
    old = TestPlan(version=1, tasks=(task("a", 20),))
    changed = TestPlan(version=2, tasks=(task("a", 30), task("b", 50)))
    with pytest.raises(PlanRejected, match="preserve"):
        validate_plan(request(), changed, old)


def test_only_completed_measured_trials_count_toward_five() -> None:
    tasks = tuple(task(str(index), 20 + index * 10) for index in range(5))
    plan = TestPlan(version=1, tasks=tasks)
    records = tuple(completed(item.id, rate=10) for item in tasks)
    assert outcome(request(), plan, records) is RunOutcome.NOT_MET_WITHIN_BUDGET
    assert outcome(request(), plan, records[:4]) is RunOutcome.RUNNING


def test_cpu_threshold_is_checked_without_tolerance_band() -> None:
    plan = TestPlan(version=1, tasks=(task("a", 50),))
    assert outcome(request(), plan, (completed("a", cpu=500),)) is RunOutcome.GOAL_MET
    assert outcome(request(), plan, (completed("a", cpu=501),)) is RunOutcome.RUNNING


def test_three_infrastructure_attempts_block_dependents_not_siblings() -> None:
    plan = TestPlan(
        version=1,
        tasks=(task("a", 20), task("dependent", 30, depends_on=("a",)), task("other", 40)),
    )
    records = tuple(infra("a", attempt) for attempt in (1, 2, 3))
    assert blocked_task_ids(plan, records) == {"a", "dependent"}
    assert tuple(item.id for item in ready_tasks(request(), plan, records)) == ("other",)
    assert outcome(request(), plan, records) is RunOutcome.RUNNING
    assert outcome(request(), plan, (*records, completed("other", rate=10))) is RunOutcome.BLOCKED


def test_blocked_attempts_do_not_consume_five_valid_trials() -> None:
    plan = TestPlan(version=1, tasks=tuple(task(str(index), 10 + index * 5) for index in range(6)))
    validate_plan(request(), plan)
    records = tuple(infra("0", attempt) for attempt in (1, 2, 3))
    assert outcome(request(), plan, records) is RunOutcome.RUNNING


def test_required_blocker_prevents_success_claim_from_independent_trial() -> None:
    plan = TestPlan(version=1, tasks=(task("required", 20), task("independent", 50)))
    records = (
        *tuple(infra("required", attempt) for attempt in (1, 2, 3)),
        completed("independent"),
    )
    assert outcome(request(), plan, records) is RunOutcome.BLOCKED
