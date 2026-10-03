"""Historical adaptive CPU prototype; the baseline executor is execution.probes."""

from __future__ import annotations

import operator
import time
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Protocol, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from kubeproof.execution.errors import TrialInfrastructureError as TrialInfrastructureError
from kubeproof.intelligence.capabilities import CapabilityCatalog, cpu_capabilities
from kubeproof.intelligence.control import (
    PlanRejected,
    next_attempt,
    outcome,
    ready_tasks,
    validate_plan,
)
from kubeproof.intelligence.model_adapter import InvalidModelPlan, Supervisor
from kubeproof.intelligence.models import (
    ConfirmedRequest,
    PlanTask,
    RunOutcome,
    TestPlan,
    TrialRecord,
    TrialStatus,
)


class TaskWorker(Protocol):
    def execute(self, request: ConfirmedRequest, task: PlanTask, attempt: int) -> TrialRecord: ...


class TrialAdmission(Protocol):
    def admit(self, request: ConfirmedRequest, task: PlanTask) -> None: ...


class Blackboard(TypedDict):
    request: dict[str, Any]
    plan: dict[str, Any] | None
    records: Annotated[list[dict[str, Any]], operator.add]
    started_at: float
    model_calls: int
    tool_calls: Annotated[int, operator.add]
    next_task_ids: list[str]
    outcome: str
    explanation: str


class WorkerInput(TypedDict):
    request: dict[str, Any]
    task: dict[str, Any]
    attempt: int


def initial_state(request: ConfirmedRequest, *, now: float | None = None) -> Blackboard:
    """Create a serializable, confirmed-only state; no natural-language draft enters execution."""
    return {
        "request": request.model_dump(mode="json"),
        "plan": None,
        "records": [],
        "started_at": time.time() if now is None else now,
        "model_calls": 0,
        "tool_calls": 0,
        "next_task_ids": [],
        "outcome": RunOutcome.RUNNING.value,
        "explanation": "",
    }


def build_graph(
    supervisor: Supervisor,
    workers: Mapping[str, TaskWorker],
    admission: TrialAdmission,
    *,
    catalog: CapabilityCatalog | None = None,
    clock: Callable[[], float] = time.time,
    checkpointer: Any = None,
) -> Any:
    """Build a bounded graph; workers and model are injected, never LLM-selected."""
    catalog = cpu_capabilities() if catalog is None else catalog
    missing_workers = catalog.names() - workers.keys()
    if missing_workers:
        raise ValueError(f"missing workers for capabilities: {sorted(missing_workers)}")

    def plan_node(state: Blackboard) -> dict[str, Any]:
        request = ConfirmedRequest.model_validate(state["request"])
        previous = TestPlan.model_validate(state["plan"]) if state["plan"] else None
        records = tuple(TrialRecord.model_validate(item) for item in state["records"])
        if state["model_calls"] >= request.budget.max_model_calls:
            return {
                "outcome": RunOutcome.BLOCKED.value,
                "explanation": "model-call budget exhausted before a valid plan",
            }
        if previous is not None and previous.version >= request.budget.max_plan_versions:
            return {
                "outcome": (
                    RunOutcome.INCONCLUSIVE.value
                    if any(record.status is TrialStatus.COMPLETED for record in records)
                    else RunOutcome.BLOCKED.value
                ),
                "explanation": "plan-version budget exhausted without reaching the goal",
            }
        try:
            proposed = supervisor.propose(request, previous, records, catalog)
            validate_plan(request, proposed, previous, catalog=catalog)
        except (InvalidModelPlan, PlanRejected) as exc:
            return {
                "model_calls": state["model_calls"] + 1,
                "outcome": RunOutcome.BLOCKED.value,
                "explanation": f"supervisor plan rejected: {exc}",
            }
        return {
            "plan": proposed.model_dump(mode="json"),
            "model_calls": state["model_calls"] + 1,
            "next_task_ids": [],
            "explanation": "",
        }

    def coordinate_node(state: Blackboard) -> dict[str, Any]:
        request = ConfirmedRequest.model_validate(state["request"])
        if state["outcome"] != RunOutcome.RUNNING.value:
            return {}
        if clock() - state["started_at"] >= request.budget.max_elapsed_seconds:
            return {
                "outcome": RunOutcome.BLOCKED.value,
                "explanation": "elapsed-time budget exhausted",
            }
        if state["plan"] is None:
            return {"outcome": RunOutcome.BLOCKED.value, "explanation": "missing plan"}
        plan = TestPlan.model_validate(state["plan"])
        records = tuple(TrialRecord.model_validate(item) for item in state["records"])
        current_outcome = outcome(request, plan, records, catalog=catalog)
        if current_outcome is not RunOutcome.RUNNING:
            return {"outcome": current_outcome.value, "next_task_ids": []}
        remaining = request.budget.max_tool_calls - state["tool_calls"]
        ready = ready_tasks(request, plan, records, catalog=catalog, limit=remaining)
        if ready:
            return {"next_task_ids": [task.id for task in ready]}
        if remaining <= 0:
            return {
                "outcome": RunOutcome.BLOCKED.value,
                "explanation": "tool-call budget exhausted",
            }
        return {"next_task_ids": []}

    def route_after_plan(state: Blackboard) -> str:
        return END if state["outcome"] != RunOutcome.RUNNING.value else "coordinate"

    def route_after_coordinate(state: Blackboard) -> str | list[Send]:
        if state["outcome"] != RunOutcome.RUNNING.value:
            return END
        task_ids = state["next_task_ids"]
        if not task_ids:
            return "plan"
        request = ConfirmedRequest.model_validate(state["request"])
        plan = TestPlan.model_validate(state["plan"])
        by_id = {task.id: task for task in plan.tasks}
        records = tuple(TrialRecord.model_validate(item) for item in state["records"])
        sends: list[Send] = []
        for task_id in task_ids:
            task = by_id[task_id]
            parameters = catalog.parse_parameters(request, task).model_dump(mode="json")
            normalized = task.model_copy(update={"parameters": parameters})
            sends.append(
                Send(
                    "execute",
                    {
                        "request": state["request"],
                        "task": normalized.model_dump(mode="json"),
                        "attempt": next_attempt(task_id, records),
                    },
                )
            )
        return sends

    def execute_node(state: WorkerInput) -> dict[str, Any]:
        request = ConfirmedRequest.model_validate(state["request"])
        task = PlanTask.model_validate(state["task"])
        catalog.parse_parameters(request, task)
        attempt = state["attempt"]
        admission.admit(request, task)
        try:
            record = workers[task.capability].execute(request, task, attempt)
        except TrialInfrastructureError as exc:
            record = TrialRecord(
                task_id=task.id,
                attempt=attempt,
                status=TrialStatus.INFRASTRUCTURE_FAILURE,
                explanation=str(exc) or "infrastructure prevented the trial",
            )
        if record.task_id != task.id or record.attempt != attempt:
            raise ValueError("worker returned a result for a different task or attempt")
        if record.observation is not None:
            record = record.model_copy(
                update={
                    "observation": catalog.parse_observation(task, record.observation).model_dump(
                        mode="json"
                    )
                }
            )
        return {"records": [record.model_dump(mode="json")], "tool_calls": 1}

    builder = StateGraph(Blackboard)
    builder.add_node("plan", plan_node)
    builder.add_node("coordinate", coordinate_node)
    builder.add_node("execute", execute_node)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges("plan", route_after_plan)
    builder.add_conditional_edges("coordinate", route_after_coordinate)
    builder.add_edge("execute", "coordinate")
    return cast(Any, builder.compile(checkpointer=checkpointer))
