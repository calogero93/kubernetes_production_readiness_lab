"""Graph tests use injected fakes and never contact a model or cluster."""

import pytest

from kubeproof.intelligence.capabilities import CapabilityCatalog
from kubeproof.intelligence.models import (
    ConfirmedRequest,
    CpuMeasurement,
    PlanTask,
    RunOutcome,
    TestPlan,
    TrialRecord,
    TrialStatus,
)
from kubeproof.intelligence.workflow import TrialInfrastructureError, build_graph, initial_state

from .test_control import mixed_catalog, probe_task, request, task


class FakeSupervisor:
    def __init__(self, plans: tuple[TestPlan, ...]):
        self.plans = plans
        self.calls = 0

    def propose(
        self,
        request: ConfirmedRequest,
        previous: TestPlan | None,
        records: tuple[TrialRecord, ...],
        catalog: CapabilityCatalog,
    ) -> TestPlan:
        del request, previous, records, catalog
        result = self.plans[self.calls]
        self.calls += 1
        return result


class FakeTool:
    def __init__(self, *, failures: dict[str, int] | None = None, achieved_rps: float = 50):
        self.failures = failures or {}
        self.achieved_rps = achieved_rps
        self.calls: list[tuple[str, int]] = []

    def execute(self, request: ConfirmedRequest, task: PlanTask, attempt: int) -> TrialRecord:
        del request
        self.calls.append((task.id, attempt))
        if attempt <= self.failures.get(task.id, 0):
            raise TrialInfrastructureError("endpoint unavailable before load")
        return TrialRecord(
            task_id=task.id,
            attempt=attempt,
            status=TrialStatus.COMPLETED,
            observation=CpuMeasurement(
                achieved_rps=self.achieved_rps,
                p95_ms=80,
                failed_requests=0,
                cpu_millicores=400,
                cpu_sample_complete=True,
                evidence_ref=f"artifact:{task.id}",
            ).model_dump(mode="json"),
            explanation="valid measured trial",
        )


class FakeAdmission:
    def admit(self, request: ConfirmedRequest, task: PlanTask) -> None:
        assert request.target_id == "cpu-fixture"
        if task.capability == "cpu_load":
            assert task.parameters["requests"] <= request.budget.max_requests_per_trial


class ProbeTool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, request: ConfirmedRequest, task: PlanTask, attempt: int) -> TrialRecord:
        del request
        self.calls.append(task.id)
        return TrialRecord(
            task_id=task.id,
            attempt=attempt,
            status=TrialStatus.COMPLETED,
            observation={"reachable": True, "evidence_ref": f"artifact:{task.id}"},
            explanation="test-only probe completed",
        )


def test_graph_accepts_one_valid_cpu_trial() -> None:
    supervisor = FakeSupervisor((TestPlan(version=1, tasks=(task("a", 50),)),))
    tool = FakeTool()
    graph = build_graph(supervisor, {"cpu_load": tool}, FakeAdmission(), clock=lambda: 0)
    state = graph.invoke(initial_state(request(), now=0), {"recursion_limit": 30})
    assert state["outcome"] == RunOutcome.GOAL_MET.value
    assert state["model_calls"] == 1
    assert state["tool_calls"] == 1
    assert tool.calls == [("a", 1)]


def test_one_plan_routes_two_capabilities_and_combines_parallel_results() -> None:
    catalog = mixed_catalog()
    supervisor = FakeSupervisor((TestPlan(version=1, tasks=(task("cpu", 50), probe_task())),))
    cpu_tool = FakeTool()
    probe_tool = ProbeTool()
    graph = build_graph(
        supervisor,
        {"cpu_load": cpu_tool, "fake_probe": probe_tool},
        FakeAdmission(),
        catalog=catalog,
        clock=lambda: 0,
    )
    state = graph.invoke(initial_state(request(), now=0), {"recursion_limit": 30})
    assert state["outcome"] == RunOutcome.GOAL_MET.value
    assert state["model_calls"] == 1
    assert state["tool_calls"] == 2
    assert {record["task_id"] for record in state["records"]} == {"cpu", "probe"}
    assert cpu_tool.calls == [("cpu", 1)]
    assert probe_tool.calls == ["probe"]


def test_worker_receives_validated_normalized_parameters() -> None:
    class InspectTool(FakeTool):
        def execute(self, request: ConfirmedRequest, task: PlanTask, attempt: int) -> TrialRecord:
            assert task.parameters == {"offered_rps": 50.0, "requests": 100}
            assert isinstance(task.parameters["requests"], int)
            return super().execute(request, task, attempt)

    proposed = task("cpu", 50).model_copy(
        update={"parameters": {"offered_rps": "50", "requests": "100"}}
    )
    supervisor = FakeSupervisor((TestPlan(version=1, tasks=(proposed,)),))
    graph = build_graph(supervisor, {"cpu_load": InspectTool()}, FakeAdmission(), clock=lambda: 0)
    state = graph.invoke(initial_state(request(), now=0), {"recursion_limit": 30})
    assert state["outcome"] == RunOutcome.GOAL_MET.value


def test_graph_replans_but_keeps_old_tasks() -> None:
    first = task("low", 20)
    second = task("target", 50)
    supervisor = FakeSupervisor(
        (TestPlan(version=1, tasks=(first,)), TestPlan(version=2, tasks=(first, second)))
    )

    class RateTool(FakeTool):
        def execute(self, request: ConfirmedRequest, task: PlanTask, attempt: int) -> TrialRecord:
            self.achieved_rps = task.parameters["offered_rps"]
            return super().execute(request, task, attempt)

    tool = RateTool()
    graph = build_graph(supervisor, {"cpu_load": tool}, FakeAdmission(), clock=lambda: 0)
    state = graph.invoke(initial_state(request(), now=0), {"recursion_limit": 40})
    assert state["outcome"] == RunOutcome.GOAL_MET.value
    assert state["plan"]["version"] == 2
    assert tool.calls == [("low", 1), ("target", 1)]


def test_graph_blocks_only_descendants_after_three_infrastructure_attempts() -> None:
    supervisor = FakeSupervisor(
        (
            TestPlan(
                version=1,
                tasks=(
                    task("broken", 20),
                    task("dependent", 30, depends_on=("broken",)),
                    task("independent", 40),
                ),
            ),
        )
    )
    tool = FakeTool(failures={"broken": 3}, achieved_rps=10)
    graph = build_graph(supervisor, {"cpu_load": tool}, FakeAdmission(), clock=lambda: 0)
    state = graph.invoke(initial_state(request(), now=0), {"recursion_limit": 50})
    assert state["outcome"] == RunOutcome.BLOCKED.value
    assert tool.calls == [
        ("broken", 1),
        ("broken", 2),
        ("broken", 3),
        ("independent", 1),
    ]


def test_invalid_plan_cannot_invoke_tool() -> None:
    supervisor = FakeSupervisor((TestPlan(version=1, tasks=(task("a", 20), task("b", 20))),))
    tool = FakeTool()
    graph = build_graph(supervisor, {"cpu_load": tool}, FakeAdmission(), clock=lambda: 0)
    state = graph.invoke(initial_state(request(), now=0), {"recursion_limit": 20})
    assert state["outcome"] == RunOutcome.BLOCKED.value
    assert "rejected" in state["explanation"]
    assert tool.calls == []


def test_admission_rejection_happens_before_tool_invocation() -> None:
    class DenyAdmission:
        def admit(self, request: ConfirmedRequest, task: PlanTask) -> None:
            del request, task
            raise PermissionError("target not approved")

    supervisor = FakeSupervisor((TestPlan(version=1, tasks=(task("a", 50),)),))
    tool = FakeTool()
    graph = build_graph(supervisor, {"cpu_load": tool}, DenyAdmission(), clock=lambda: 0)
    with pytest.raises(PermissionError, match="not approved"):
        graph.invoke(initial_state(request(), now=0), {"recursion_limit": 20})
    assert tool.calls == []


def test_plan_budget_stops_replanning_after_a_negative_trial() -> None:
    constrained = request().model_copy(
        update={"budget": request().budget.model_copy(update={"max_plan_versions": 1})}
    )
    supervisor = FakeSupervisor((TestPlan(version=1, tasks=(task("low", 20),)),))
    tool = FakeTool(achieved_rps=20)
    graph = build_graph(supervisor, {"cpu_load": tool}, FakeAdmission(), clock=lambda: 0)
    state = graph.invoke(initial_state(constrained, now=0), {"recursion_limit": 30})
    assert state["outcome"] == RunOutcome.INCONCLUSIVE.value
    assert "plan-version budget" in state["explanation"]
    assert supervisor.calls == 1
    assert tool.calls == [("low", 1)]
