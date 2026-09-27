"""Typed CPU intake and capability-neutral plan/record envelopes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kubeproof.benchmarks.cpu_http.app import MAX_ITERATIONS


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CpuGoal(StrictModel):
    target_rps: float = Field(gt=0, le=200, description="Minimum verified request throughput.")
    max_p95_ms: float = Field(gt=0, description="Maximum acceptable p95 response latency.")
    max_failed_requests: int = Field(ge=0, description="Maximum failed or unsent requests.")
    max_cpu_millicores: int = Field(
        gt=0, description="Maximum observed per-Pod CPU sample, in millicores."
    )


class RunBudget(StrictModel):
    max_plan_versions: int = Field(ge=1, description="Hard cap on supervisor plan versions.")
    max_elapsed_seconds: int = Field(ge=1, description="Hard wall-clock cap for the run.")
    max_model_calls: int = Field(ge=1, description="Hard cap on model invocations.")
    max_tool_calls: int = Field(ge=1, description="Hard cap on admitted tool invocations.")
    max_requests_per_trial: int = Field(
        ge=1, le=10_000, description="Maximum scheduled requests in one CPU trial."
    )


class ConfirmedRequest(StrictModel):
    schema_version: Literal["1"] = Field(default="1", description="Request schema version.")
    environment_id: str = Field(min_length=1, description="User-authorized sandbox identity.")
    target_id: str = Field(min_length=1, description="Workload identity within the sandbox.")
    work_iterations: int = Field(
        ge=1, le=MAX_ITERATIONS, description="Fixed CPU work per request across all trials."
    )
    goal: CpuGoal = Field(description="Measured objective and resource constraint.")
    budget: RunBudget = Field(description="Finite execution and AI planning budget.")


class CpuGoalDraft(StrictModel):
    target_rps: float | None = Field(default=None, description="Extracted throughput objective.")
    max_p95_ms: float | None = Field(default=None, description="Extracted latency limit.")
    max_failed_requests: int | None = Field(
        default=None, description="Extracted failed-request allowance."
    )
    max_cpu_millicores: int | None = Field(
        default=None, description="Extracted company CPU constraint."
    )


class RunBudgetDraft(StrictModel):
    max_plan_versions: int | None = Field(default=None, description="Extracted plan-version cap.")
    max_elapsed_seconds: int | None = Field(default=None, description="Extracted run-time cap.")
    max_model_calls: int | None = Field(default=None, description="Extracted model-call cap.")
    max_tool_calls: int | None = Field(default=None, description="Extracted tool-call cap.")
    max_requests_per_trial: int | None = Field(
        default=None, description="Extracted per-trial request cap."
    )


class RequestDraft(StrictModel):
    """Untrusted extraction that cannot be used as an executable request."""

    environment_id: str | None = Field(default=None, description="Named authorized environment.")
    target_id: str | None = Field(default=None, description="Named workload under test.")
    work_iterations: int | None = Field(default=None, description="Fixed CPU work definition.")
    goal: CpuGoalDraft = Field(
        default_factory=CpuGoalDraft, description="Goal and resource fields found in the text."
    )
    budget: RunBudgetDraft = Field(
        default_factory=RunBudgetDraft, description="Explicit run-budget fields found in the text."
    )
    source_fragments: dict[str, str] = Field(
        default_factory=dict,
        description="Exact input snippets keyed by dotted field path for each extracted value.",
    )
    ambiguous_statements: tuple[str, ...] = Field(
        default=(), description="Statements needing user clarification."
    )
    unmapped_statements: tuple[str, ...] = Field(
        default=(), description="Relevant text that does not fit the current request schema."
    )


class CpuLoadParameters(StrictModel):
    offered_rps: float = Field(gt=0, le=200, description="Fixed offered request rate.")
    requests: int = Field(ge=1, le=10_000, description="Number of scheduled requests.")


class PlanTask(StrictModel):
    id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        description="Stable, path-safe task ID within a plan and all its revisions.",
    )
    capability: str = Field(min_length=1, description="Registered test capability identifier.")
    parameters: dict[str, Any] = Field(
        description="Capability-specific parameters, validated against the registered schema."
    )
    depends_on: tuple[str, ...] = Field(
        default=(), description="Task IDs that must complete before this test can run."
    )
    required: bool = Field(
        default=True, description="Whether this result is required to answer the objective."
    )
    rationale: str = Field(min_length=1, description="Why the supervisor selected this trial.")


class TestPlan(StrictModel):
    __test__: ClassVar[bool] = False  # Imported model, not a pytest test class.

    schema_version: Literal["1"] = Field(default="1", description="Plan schema version.")
    version: int = Field(ge=1, description="Monotonically increasing plan version.")
    tasks: tuple[PlanTask, ...] = Field(
        min_length=1, description="Complete task list including earlier versions' tasks."
    )


class TrialStatus(StrEnum):
    COMPLETED = "completed"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"


class CpuMeasurement(StrictModel):
    achieved_rps: float = Field(ge=0, description="Verified successful throughput.")
    p95_ms: float | None = Field(
        default=None, description="p95 latency of successful responses, if any."
    )
    failed_requests: int = Field(ge=0, description="Failed responses or requests not sent.")
    cpu_millicores: int | None = Field(
        default=None, ge=0, description="Maximum observed per-Pod CPU sample."
    )
    cpu_sample_complete: bool = Field(
        description="Whether the required Pod CPU samples were collected."
    )
    evidence_ref: str = Field(min_length=1, description="Immutable measurement artifact reference.")

    @model_validator(mode="after")
    def require_complete_cpu_value(self) -> CpuMeasurement:
        if self.cpu_sample_complete and self.cpu_millicores is None:
            raise ValueError("complete CPU sampling requires a CPU value")
        return self


class TrialRecord(StrictModel):
    task_id: str = Field(description="Task that produced this attempt.")
    attempt: int = Field(ge=1, le=3, description="Execution attempt number for this task.")
    status: TrialStatus = Field(description="Whether the test completed technically.")
    observation: dict[str, Any] | None = Field(
        default=None,
        description="Capability-specific observation with immutable evidence reference.",
    )
    explanation: str = Field(min_length=1, description="Outcome or infrastructure error detail.")

    @model_validator(mode="after")
    def status_matches_observation(self) -> TrialRecord:
        if (self.status is TrialStatus.COMPLETED) != (self.observation is not None):
            raise ValueError(
                "completed attempts require observations; failed attempts cannot have them"
            )
        return self


class RunOutcome(StrEnum):
    RUNNING = "running"
    GOAL_MET = "goal_met"
    NOT_MET_WITHIN_BUDGET = "not_met_within_budget"
    INCONCLUSIVE = "inconclusive"
    BLOCKED = "blocked"


MAX_VALID_TRIALS = 5
MAX_EXECUTION_ATTEMPTS = 3
