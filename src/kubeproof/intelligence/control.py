"""Deterministic admission, scheduling and outcome rules for one mixed plan."""

from __future__ import annotations

import json
from collections.abc import Iterable

from kubeproof.intelligence.capabilities import CapabilityCatalog, cpu_capabilities
from kubeproof.intelligence.models import (
    MAX_EXECUTION_ATTEMPTS,
    MAX_VALID_TRIALS,
    ConfirmedRequest,
    PlanTask,
    RunOutcome,
    TestPlan,
    TrialRecord,
    TrialStatus,
)


class PlanRejected(ValueError):
    """A supervisor proposal cannot be admitted for execution."""


def validate_plan(
    request: ConfirmedRequest,
    plan: TestPlan,
    previous: TestPlan | None = None,
    *,
    catalog: CapabilityCatalog | None = None,
) -> None:
    """Validate a full plan; revisions may only append tasks."""
    catalog = cpu_capabilities() if catalog is None else catalog
    expected_version = 1 if previous is None else previous.version + 1
    if plan.version != expected_version:
        raise PlanRejected(f"expected plan version {expected_version}")
    if len(plan.tasks) > request.budget.max_tool_calls:
        raise PlanRejected("plan contains more tasks than the tool-call budget can start")
    if previous is not None and plan.tasks[: len(previous.tasks)] != previous.tasks:
        raise PlanRejected("plan revisions must preserve existing tasks")

    by_id: dict[str, PlanTask] = {}
    signatures: set[tuple[str, str]] = set()
    for task in plan.tasks:
        if task.id in by_id:
            raise PlanRejected(f"duplicate task ID: {task.id}")
        try:
            parameters = catalog.parse_parameters(request, task)
            spec = catalog.get(task.capability)
        except ValueError as exc:
            raise PlanRejected(str(exc)) from exc
        signature = (
            task.capability,
            json.dumps(parameters.model_dump(mode="json"), sort_keys=True),
        )
        if spec.distinct_parameters and signature in signatures:
            raise PlanRejected(f"{task.capability} trials must use distinct parameter sets")
        signatures.add(signature)
        by_id[task.id] = task

    for task in plan.tasks:
        if len(task.depends_on) != len(set(task.depends_on)):
            raise PlanRejected(f"task {task.id} has duplicate dependencies")
        for dependency in task.depends_on:
            if dependency not in by_id:
                raise PlanRejected(f"task {task.id} has an unknown dependency: {dependency}")

    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise PlanRejected("plan contains a dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].depends_on:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)


def validate_records(
    plan: TestPlan, records: Iterable[TrialRecord], *, catalog: CapabilityCatalog | None = None
) -> None:
    """Reject forged, duplicated or out-of-order task results."""
    catalog = cpu_capabilities() if catalog is None else catalog
    known = {task.id: task for task in plan.tasks}
    seen: dict[str, list[TrialRecord]] = {}
    for record in records:
        if record.task_id not in known:
            raise ValueError(f"record references unknown task: {record.task_id}")
        prior = seen.setdefault(record.task_id, [])
        if record.attempt != len(prior) + 1:
            raise ValueError(f"attempts for {record.task_id} are not sequential")
        if prior and prior[-1].status is TrialStatus.COMPLETED:
            raise ValueError(f"task {record.task_id} ran after completion")
        if len(prior) >= MAX_EXECUTION_ATTEMPTS:
            raise ValueError(f"task {record.task_id} exceeded its execution-attempt budget")
        if record.observation is not None:
            catalog.parse_observation(known[record.task_id], record.observation)
        prior.append(record)


def _records_by_task(records: Iterable[TrialRecord]) -> dict[str, list[TrialRecord]]:
    grouped: dict[str, list[TrialRecord]] = {}
    for record in records:
        grouped.setdefault(record.task_id, []).append(record)
    return grouped


def blocked_task_ids(plan: TestPlan, records: Iterable[TrialRecord]) -> set[str]:
    """A blocked task also blocks descendants, never independent siblings."""
    grouped = _records_by_task(records)
    blocked = {
        task_id
        for task_id, attempts in grouped.items()
        if len(attempts) >= MAX_EXECUTION_ATTEMPTS
        and attempts[-1].status is TrialStatus.INFRASTRUCTURE_FAILURE
    }
    changed = True
    while changed:
        before = len(blocked)
        blocked.update(
            task.id for task in plan.tasks if any(dep in blocked for dep in task.depends_on)
        )
        changed = len(blocked) != before
    return blocked


def ready_tasks(
    request: ConfirmedRequest,
    plan: TestPlan,
    records: Iterable[TrialRecord],
    *,
    catalog: CapabilityCatalog | None = None,
    limit: int | None = None,
) -> tuple[PlanTask, ...]:
    """Return dependency-ready tasks with disjoint deterministic resource claims."""
    catalog = cpu_capabilities() if catalog is None else catalog
    if limit is not None and limit <= 0:
        return ()
    materialized = tuple(records)
    validate_records(plan, materialized, catalog=catalog)
    grouped = _records_by_task(materialized)
    blocked = blocked_task_ids(plan, materialized)
    completed = {
        task_id
        for task_id, attempts in grouped.items()
        if attempts[-1].status is TrialStatus.COMPLETED
    }
    ready = [
        task
        for task in plan.tasks
        if task.id not in blocked
        and task.id not in completed
        and all(dep in completed for dep in task.depends_on)
    ]
    selected: list[PlanTask] = []
    claimed: set[str] = set()
    for task in ready:
        resources = catalog.claims(request, task)
        if not claimed.isdisjoint(resources):
            continue
        selected.append(task)
        claimed.update(resources)
        if limit is not None and len(selected) >= limit:
            break
    return tuple(selected)


def next_attempt(task_id: str, records: Iterable[TrialRecord]) -> int:
    return 1 + sum(record.task_id == task_id for record in records)


def outcome(
    request: ConfirmedRequest,
    plan: TestPlan,
    records: Iterable[TrialRecord],
    *,
    catalog: CapabilityCatalog | None = None,
) -> RunOutcome:
    """Compute a scoped terminal outcome from immutable attempts."""
    catalog = cpu_capabilities() if catalog is None else catalog
    materialized = tuple(records)
    validate_records(plan, materialized, catalog=catalog)
    by_id = {task.id: task for task in plan.tasks}
    completed = [record for record in materialized if record.status is TrialStatus.COMPLETED]
    completed_ids = {record.task_id for record in completed}
    blocked = blocked_task_ids(plan, materialized)
    if any(task.required and task.id in blocked for task in plan.tasks):
        return (
            RunOutcome.RUNNING
            if ready_tasks(request, plan, materialized, catalog=catalog)
            else RunOutcome.BLOCKED
        )
    goal_met = False
    valid_goal_trials = 0
    for record in completed:
        task = by_id[record.task_id]
        spec = catalog.get(task.capability)
        if spec.meets_goal is None or record.observation is None:
            continue
        valid_goal_trials += 1
        observation = catalog.parse_observation(task, record.observation)
        goal_met |= spec.meets_goal(request, task, observation)
    if goal_met and all(not task.required or task.id in completed_ids for task in plan.tasks):
        return RunOutcome.GOAL_MET
    if not goal_met and valid_goal_trials >= MAX_VALID_TRIALS:
        return RunOutcome.NOT_MET_WITHIN_BUDGET
    return RunOutcome.RUNNING
