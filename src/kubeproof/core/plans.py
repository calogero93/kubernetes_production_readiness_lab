"""Provider-neutral, deeply immutable plans made of typed probe tasks."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from kubeproof.core.probe_options import HttpProbeOptions, StrictModel


class PlanBudget(StrictModel):
    max_tasks: int = Field(default=16, ge=1, le=32, strict=True)
    max_elapsed_seconds: int = Field(default=900, ge=1, le=1800, strict=True)
    max_parallel_tasks: int = Field(default=1, ge=1, le=4, strict=True)


class TaskBase(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    probe_version: Literal["1"] = "1"
    depends_on: tuple[str, ...] = ()
    when: Literal["completed", "passed", "failed"] = "completed"
    rationale: str = Field(min_length=1, max_length=1000)


class HttpTask(TaskBase):
    capability: Literal["http_service"] = "http_service"
    parameters: HttpProbeOptions


class ResourceParameters(StrictModel):
    duration_seconds: int = Field(default=30, ge=0, le=600, strict=True)


class ResourceTask(TaskBase):
    capability: Literal["resource_sample"] = "resource_sample"
    parameters: ResourceParameters = ResourceParameters()


class DnsParameters(StrictModel):
    scope: Literal["retained_product_queries"] = "retained_product_queries"


class DnsTask(TaskBase):
    capability: Literal["dns_observation"] = "dns_observation"
    parameters: DnsParameters = DnsParameters()


class RecoveryParameters(StrictModel):
    max_targets: int = Field(default=1, ge=0, le=5, strict=True)


class RecoveryTask(TaskBase):
    capability: Literal["pod_recovery"] = "pod_recovery"
    parameters: RecoveryParameters = RecoveryParameters()


class CpuParameters(StrictModel):
    work_iterations: int = Field(ge=1, le=2_000_000, strict=True)
    offered_rps: float = Field(gt=0, le=200, allow_inf_nan=False, strict=True)
    requests: int = Field(ge=1, le=10_000, strict=True)
    min_success_rps: float = Field(gt=0, le=200, allow_inf_nan=False, strict=True)
    max_p95_ms: float = Field(gt=0, allow_inf_nan=False, strict=True)
    max_failed_requests: int = Field(ge=0, le=10_000, strict=True)
    max_cpu_millicores: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def bounded_load(self) -> CpuParameters:
        if not 20 <= self.requests / self.offered_rps <= 90:
            raise ValueError("CPU fixture trials must schedule 20-90 seconds of load")
        if self.max_failed_requests >= self.requests:
            raise ValueError("CPU allowance must require at least one successful request")
        return self


class CpuTask(TaskBase):
    capability: Literal["cpu_load"] = "cpu_load"
    parameters: CpuParameters


ProbeTask = Annotated[
    HttpTask | ResourceTask | DnsTask | RecoveryTask | CpuTask, Field(discriminator="capability")
]


class EvaluationPlan(StrictModel):
    schema_version: Literal["1"] = "1"
    catalog_version: Literal["1"] = "1"
    origin: Literal["deterministic", "user", "ai"] = "user"
    objective: str = Field(min_length=1, max_length=4000)
    budget: PlanBudget = PlanBudget()
    tasks: tuple[ProbeTask, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def valid_graph(self) -> EvaluationPlan:
        if len(self.tasks) > self.budget.max_tasks:
            raise ValueError("plan exceeds its task budget")
        tasks = {task.id: task for task in self.tasks}
        if len(tasks) != len(self.tasks):
            raise ValueError("duplicate plan task id")
        for task in self.tasks:
            if len(set(task.depends_on)) != len(task.depends_on):
                raise ValueError(f"duplicate dependencies for {task.id}")
            if set(task.depends_on) - tasks.keys():
                raise ValueError(f"unknown dependencies for {task.id}")
            if task.when != "completed" and not task.depends_on:
                raise ValueError("conditional tasks require dependencies")
        visited: set[str] = set()
        active: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in active:
                raise ValueError("cyclic plan dependencies")
            if task_id in visited:
                return
            active.add(task_id)
            for dependency in tasks[task_id].depends_on:
                visit(dependency)
            active.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)
        return self

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def default_plan(
    *,
    http: HttpProbeOptions | None = None,
    steady_state_seconds: int = 60,
    max_recovery_targets: int = 5,
) -> EvaluationPlan:
    tasks: list[HttpTask | ResourceTask | DnsTask | RecoveryTask] = []
    if http is not None:
        tasks.append(HttpTask(id="http", parameters=http, rationale="Confirmed HTTP requirements."))
    tasks.append(
        ResourceTask(
            id="resources",
            parameters=ResourceParameters(duration_seconds=steady_state_seconds),
            rationale="Observe sampled product resource usage against the company profile.",
        )
    )
    tasks.append(DnsTask(id="network", rationale="Inspect retained DNS evidence."))
    tasks.append(
        RecoveryTask(
            id="recovery",
            parameters=RecoveryParameters(max_targets=max_recovery_targets),
            rationale="Measure eligible Deployment Pod replacement readiness.",
        )
    )
    return EvaluationPlan(
        origin="deterministic", objective="Qualify the selected product.", tasks=tuple(tasks)
    )


class TaskExecution(StrictModel):
    task_id: str
    state: Literal["completed", "infrastructure_failure", "cancelled", "skipped", "not_tested"]
    check_ids: tuple[str, ...] = ()
    explanation: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class PlanExecution(StrictModel):
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tasks: tuple[TaskExecution, ...]
