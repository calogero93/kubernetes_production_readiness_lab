"""Typed capability catalog shared by planning, validation and dispatch."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from kubeproof.intelligence.models import (
    ConfirmedRequest,
    CpuLoadParameters,
    CpuMeasurement,
    PlanTask,
)


@dataclass(frozen=True)
class CapabilitySpec:
    """A registered operation, not permission to execute it."""

    name: str
    description: str
    parameters_model: type[BaseModel]
    observation_model: type[BaseModel]
    validate_parameters: Callable[[ConfirmedRequest, BaseModel], None]
    validate_observation: Callable[[BaseModel], None]
    resource_keys: Callable[[ConfirmedRequest, BaseModel], frozenset[str]]
    meets_goal: Callable[[ConfirmedRequest, PlanTask, BaseModel], bool] | None = None
    distinct_parameters: bool = False


class CapabilityCatalog:
    """Only registered capabilities may appear in an admitted plan."""

    def __init__(self, specs: Iterable[CapabilitySpec]):
        self._specs: dict[str, CapabilitySpec] = {}
        for spec in specs:
            if not spec.name or spec.name in self._specs:
                raise ValueError(f"duplicate or empty capability name: {spec.name!r}")
            self._specs[spec.name] = spec

    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def get(self, name: str) -> CapabilitySpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ValueError(f"unregistered capability: {name}") from exc

    def parse_parameters(self, request: ConfirmedRequest, task: PlanTask) -> BaseModel:
        spec = self.get(task.capability)
        try:
            parameters = spec.parameters_model.model_validate(task.parameters)
        except ValidationError as exc:
            raise ValueError(f"invalid parameters for {task.id}: {exc}") from exc
        spec.validate_parameters(request, parameters)
        return parameters

    def parse_observation(self, task: PlanTask, value: dict[str, Any]) -> BaseModel:
        spec = self.get(task.capability)
        try:
            observation = spec.observation_model.model_validate(value)
        except ValidationError as exc:
            raise ValueError(f"invalid observation for {task.id}: {exc}") from exc
        spec.validate_observation(observation)
        return observation

    def claims(self, request: ConfirmedRequest, task: PlanTask) -> frozenset[str]:
        spec = self.get(task.capability)
        return spec.resource_keys(request, self.parse_parameters(request, task))

    def planning_context(self) -> list[dict[str, Any]]:
        """Describe available operations without granting execution authority."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters_schema": spec.parameters_model.model_json_schema(),
            }
            for spec in self._specs.values()
        ]


def _validate_cpu(request: ConfirmedRequest, raw: BaseModel) -> None:
    parameters = CpuLoadParameters.model_validate(raw)
    if parameters.requests > request.budget.max_requests_per_trial:
        raise ValueError("CPU trial exceeds the per-trial request budget")


def _validate_live_cpu(request: ConfirmedRequest, raw: BaseModel) -> None:
    _validate_cpu(request, raw)
    parameters = CpuLoadParameters.model_validate(raw)
    if not 20 <= parameters.requests / parameters.offered_rps <= 90:
        raise ValueError("live CPU trials must schedule between 20 and 90 seconds of load")


def _cpu_resources(request: ConfirmedRequest, raw: BaseModel) -> frozenset[str]:
    CpuLoadParameters.model_validate(raw)
    return frozenset({f"{request.environment_id}:{request.target_id}:load"})


def _validate_cpu_observation(raw: BaseModel) -> None:
    measurement = CpuMeasurement.model_validate(raw)
    if not measurement.cpu_sample_complete:
        raise ValueError("a completed CPU trial requires complete CPU sampling")


def _cpu_meets_goal(request: ConfirmedRequest, task: PlanTask, raw: BaseModel) -> bool:
    parameters = CpuLoadParameters.model_validate(task.parameters)
    measurement = CpuMeasurement.model_validate(raw)
    goal = request.goal
    return bool(
        parameters.offered_rps >= goal.target_rps
        and measurement.achieved_rps >= goal.target_rps
        and measurement.p95_ms is not None
        and measurement.p95_ms <= goal.max_p95_ms
        and measurement.failed_requests <= goal.max_failed_requests
        and measurement.cpu_sample_complete
        and measurement.cpu_millicores is not None
        and measurement.cpu_millicores <= goal.max_cpu_millicores
    )


def cpu_capabilities(*, live: bool = False) -> CapabilityCatalog:
    """Current executable catalog; more capabilities require explicit registration."""
    return CapabilityCatalog(
        (
            CapabilitySpec(
                name="cpu_load",
                description=(
                    "Measure a bounded CPU-bound HTTP workload at a fixed request rate."
                    + (" A live trial must last 20-90 seconds." if live else "")
                ),
                parameters_model=CpuLoadParameters,
                observation_model=CpuMeasurement,
                validate_parameters=_validate_live_cpu if live else _validate_cpu,
                validate_observation=_validate_cpu_observation,
                resource_keys=_cpu_resources,
                meets_goal=_cpu_meets_goal,
                distinct_parameters=True,
            ),
        )
    )
