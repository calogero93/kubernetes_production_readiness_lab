"""Plan one fixture CPU experiment, run its worker and report the evidence."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
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
from kubeproof.execution.cpu_fixture import validate_fixture_resources
from kubeproof.execution.cpu_worker import CpuLoadWorker, prepare_fixture_image
from kubeproof.execution.helm import HelmRenderRequest
from kubeproof.execution.kubernetes import ClusterReader
from kubeproof.execution.runtime import ExperimentEvidence
from kubeproof.intelligence.capabilities import CapabilityCatalog, cpu_capabilities
from kubeproof.intelligence.control import PlanRejected, validate_plan, validate_records
from kubeproof.intelligence.model_adapter import LangChainSupervisor
from kubeproof.intelligence.model_config import create_chat_model
from kubeproof.intelligence.models import (
    MAX_EXECUTION_ATTEMPTS,
    ConfirmedRequest,
    CpuMeasurement,
    PlanTask,
    RunOutcome,
    TestPlan,
    TrialRecord,
    TrialStatus,
)
from kubeproof.intelligence.workflow import TrialInfrastructureError, build_graph, initial_state

_CHECK_ID = "runtime.cpu_load"
_TITLE = "Bounded real CPU load on one fixture Pod"


@dataclass(frozen=True)
class _CpuRun:
    plan: TestPlan | None
    records: tuple[TrialRecord, ...]
    outcome: RunOutcome
    explanation: str = ""


def _pilot_plan(request: ConfirmedRequest) -> TestPlan:
    return TestPlan(
        version=1,
        tasks=(
            PlanTask(
                id="cpu-pilot",
                capability="cpu_load",
                parameters={
                    "offered_rps": request.goal.target_rps,
                    "requests": math.ceil(request.goal.target_rps * 30),
                },
                rationale="Deterministic 30-second baseline at the confirmed target rate.",
            ),
        ),
    )


def _run_pilot(
    request: ConfirmedRequest, catalog: CapabilityCatalog, worker: CpuLoadWorker
) -> _CpuRun:
    """Run one admitted baseline, retrying only infrastructure failures."""
    plan = _pilot_plan(request)
    try:
        validate_plan(request, plan, catalog=catalog)
    except PlanRejected as exc:
        return _CpuRun(plan, (), RunOutcome.BLOCKED, f"pilot plan rejected: {exc}")

    task = plan.tasks[0]
    records: list[TrialRecord] = []
    started = time.monotonic()
    for attempt in range(1, min(MAX_EXECUTION_ATTEMPTS, request.budget.max_tool_calls) + 1):
        if time.monotonic() - started >= request.budget.max_elapsed_seconds:
            return _CpuRun(
                plan, tuple(records), RunOutcome.BLOCKED, "elapsed-time budget exhausted"
            )
        catalog.parse_parameters(request, task)  # Check the exact action before each attempt.
        try:
            record = worker.execute(request, task, attempt)
        except TrialInfrastructureError as exc:
            record = TrialRecord(
                task_id=task.id,
                attempt=attempt,
                status=TrialStatus.INFRASTRUCTURE_FAILURE,
                explanation=str(exc) or "infrastructure prevented the trial",
            )
        records.append(record)
        validate_records(plan, records, catalog=catalog)
        if record.status is TrialStatus.COMPLETED:
            assert record.observation is not None
            measurement = catalog.parse_observation(task, record.observation)
            meets_goal = catalog.get(task.capability).meets_goal
            if meets_goal is not None and meets_goal(request, task, measurement):
                return _CpuRun(plan, tuple(records), RunOutcome.GOAL_MET)
            return _CpuRun(
                plan,
                tuple(records),
                RunOutcome.INCONCLUSIVE,
                "Pilot trial completed; the goal was not met in this single trial.",
            )
    return _CpuRun(
        plan, tuple(records), RunOutcome.BLOCKED, "pilot ended without a valid CPU measurement"
    )


class _Admission:
    def __init__(self, catalog: CapabilityCatalog):
        self.catalog = catalog

    def admit(self, request: ConfirmedRequest, task: PlanTask) -> None:
        self.catalog.parse_parameters(request, task)


def _observations(
    request: ConfirmedRequest,
    catalog: CapabilityCatalog,
    plan: TestPlan | None,
    records: tuple[TrialRecord, ...],
    worker: CpuLoadWorker,
    namespace: str,
) -> tuple[Observation, ...]:
    tasks = {task.id: task for task in plan.tasks} if plan else {}
    meets_goal = catalog.get("cpu_load").meets_goal
    observations = []
    for index, record in enumerate(records, start=1):
        task = tasks.get(record.task_id)
        pod_name = worker.pod_names.get((record.task_id, record.attempt))
        passed = (
            meets_goal(request, task, CpuMeasurement.model_validate(record.observation))
            if task is not None and record.observation is not None and meets_goal is not None
            else None
        )
        observations.append(
            Observation(
                id=f"cpu-obs-{index:05d}",
                check_id=_CHECK_ID,
                source_class=SourceClass.RUNTIME,
                observation_type="cpu.bounded_load_trial",
                summary=record.explanation,
                resource=(
                    ResourceRef(api_version="v1", kind="Pod", namespace=namespace, name=pod_name)
                    if pod_name is not None
                    else None
                ),
                data={
                    "task_id": record.task_id,
                    "attempt": record.attempt,
                    "status": record.status.value,
                    "parameters": task.parameters if task is not None else None,
                    "measurement": record.observation,
                    "goal": request.goal.model_dump(mode="json"),
                    "meets_goal": passed,
                },
            )
        )
    return tuple(observations)


def _report(
    request: ConfirmedRequest,
    catalog: CapabilityCatalog,
    worker: CpuLoadWorker,
    namespace: str,
    run: _CpuRun,
) -> ExperimentEvidence:
    """Turn a CPU run into one traceable, sealed-bundle contribution."""
    plan, records, result = run.plan, run.records, run.outcome
    observations = _observations(request, catalog, plan, records, worker, namespace)

    findings = (
        (
            Finding(
                id="cpu-finding-00001",
                check_id=_CHECK_ID,
                severity=Severity.BLOCKER,
                title="CPU goal was not met within the trial budget",
                description=(
                    "Five valid parameterized CPU trials did not satisfy the confirmed goal."
                ),
                observation_ids=tuple(item.id for item in observations),
            ),
        )
        if result is RunOutcome.NOT_MET_WITHIN_BUDGET and observations
        else ()
    )
    check = CheckResult(
        id=_CHECK_ID,
        title=_TITLE,
        execution_status=(
            ExecutionStatus.COMPLETED
            if any(record.status is TrialStatus.COMPLETED for record in records)
            else ExecutionStatus.INFRASTRUCTURE_FAILURE
            if records
            and all(record.status is TrialStatus.INFRASTRUCTURE_FAILURE for record in records)
            else ExecutionStatus.COMPLETED
        ),
        assessment=(
            Assessment.PASS
            if result is RunOutcome.GOAL_MET
            else Assessment.FAIL
            if result is RunOutcome.NOT_MET_WITHIN_BUDGET
            else Assessment.INCONCLUSIVE
        ),
        observation_ids=tuple(item.id for item in observations),
        finding_ids=tuple(item.id for item in findings),
        explanation=run.explanation or None,
    )
    artifacts = dict(worker.artifacts)
    artifacts["artifacts/cpu/plan-and-state.json"] = (
        json.dumps(
            {
                "confirmed_request": request.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json") if plan else None,
                "records": [item.model_dump(mode="json") for item in records],
                "outcome": result.value,
                "explanation": run.explanation,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return ExperimentEvidence((check,), observations, findings, artifacts)


class CpuLiveExperiment:
    """Small entry point read in order: validate, prepare, run, report."""

    check_id = _CHECK_ID
    title = _TITLE

    def __init__(self, request: ConfirmedRequest, *, use_ai: bool = False) -> None:
        self.request = request
        self.catalog = cpu_capabilities(live=True)
        self.supervisor = LangChainSupervisor(create_chat_model()) if use_ai else None
        if not use_ai and request.budget.max_plan_versions != 1:
            raise ValueError("pilot mode requires max_plan_versions=1")

    def validate_resources(
        self, resources: tuple[dict[str, Any], ...], request: HelmRenderRequest
    ) -> None:
        validate_fixture_resources(resources, request, self.request)

    def prepare(self, cluster_name: str, kubeconfig: Path) -> None:
        del kubeconfig
        prepare_fixture_image(cluster_name)

    def run(
        self,
        cluster: ClusterReader,
        kubeconfig: Path,
        namespace: str,
        cancel_if: Callable[[], bool],
    ) -> ExperimentEvidence:
        worker = CpuLoadWorker(cluster, kubeconfig, namespace, cancel_if)
        if self.supervisor is None:
            run = _run_pilot(self.request, self.catalog, worker)
        else:
            graph = build_graph(
                self.supervisor,
                {"cpu_load": worker},
                _Admission(self.catalog),
                catalog=self.catalog,
            )
            state = graph.invoke(initial_state(self.request), {"recursion_limit": 80})
            run = _CpuRun(
                plan=TestPlan.model_validate(state["plan"]) if state["plan"] else None,
                records=tuple(TrialRecord.model_validate(item) for item in state["records"]),
                outcome=RunOutcome(state["outcome"]),
                explanation=state["explanation"],
            )
        return _report(self.request, self.catalog, worker, namespace, run)
