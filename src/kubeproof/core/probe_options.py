"""Shared typed parameters for registered probes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HttpProbeOptions(StrictModel):
    service: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$",
        description="Rendered selector-based Service to probe in the product namespace.",
    )
    port: int = Field(
        default=80,
        ge=1,
        le=65535,
        strict=True,
        description="Declared TCP Service port used by the HTTP probe.",
    )
    path: str = Field(
        default="/healthz",
        max_length=256,
        pattern=r"^/[a-zA-Z0-9/_.~-]*$",
        description="GET path, without query strings, credentials or redirect following.",
    )
    requests: int = Field(
        default=10,
        ge=1,
        le=50,
        strict=True,
        description="Number of sequential GET requests; this is not a throughput benchmark.",
    )
    expected_status: int = Field(
        default=200,
        ge=100,
        le=599,
        strict=True,
        description="Exact HTTP status code considered successful.",
    )
    max_failed_requests: int = Field(
        default=0,
        ge=0,
        le=50,
        strict=True,
        description="Allowed count of network errors and unexpected HTTP status codes.",
    )
    max_p95_ms: float | None = Field(
        default=None,
        gt=0,
        strict=True,
        allow_inf_nan=False,
        description="Optional maximum p95 milliseconds, including failed-request durations.",
    )

    @model_validator(mode="after")
    def bounded_request(self) -> HttpProbeOptions:
        if "//" in self.path:
            raise ValueError("HTTP probe path must not contain //")
        if self.max_failed_requests >= self.requests:
            raise ValueError("max_failed_requests must be smaller than the request count")
        return self
