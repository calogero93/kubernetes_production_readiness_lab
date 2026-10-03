"""AI commentary is separate from measured observations and deterministic findings."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from kubeproof.core.probe_options import StrictModel


class InterpretationPoint(StrictModel):
    kind: Literal["observed", "hypothesis", "recommendation"]
    explanation: str = Field(min_length=1, max_length=4000)
    observation_ids: tuple[str, ...] = Field(
        default=(),
        description="Existing evidence IDs. At least one observation_ids or check_ids is required.",
    )
    check_ids: tuple[str, ...] = Field(
        default=(),
        description="Existing check IDs. At least one observation_ids or check_ids is required.",
    )
    suggested_manifest_change: str | None = Field(
        default=None,
        max_length=4000,
        description="Only for kind=recommendation; use null for observed or hypothesis points.",
    )
    verification: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def grounded(self) -> InterpretationPoint:
        if not self.observation_ids and not self.check_ids:
            raise ValueError("AI explanations must reference existing observations or checks")
        if self.suggested_manifest_change and self.kind != "recommendation":
            raise ValueError("manifest suggestions must be labeled recommendations")
        return self


class AIInterpretation(StrictModel):
    status: Literal["completed", "unavailable"] = "completed"
    model_id: str | None = None
    points: tuple[InterpretationPoint, ...] = Field(default=(), max_length=32)
    limitations: tuple[str, ...] = Field(default=(), max_length=32)
    error: str | None = None
