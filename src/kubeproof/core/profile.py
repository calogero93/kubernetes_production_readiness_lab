"""Strict v0.1 organization-constraint profile."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import ConfigDict, Field, field_validator

from kubeproof.core.domain import StrictModel
from kubeproof.core.quantities import InvalidQuantity, parse_quantity


class SecurityConstraints(StrictModel):
    allow_cluster_admin: bool | None = None
    allow_wildcard_rbac: bool | None = None
    allow_privileged: bool | None = None
    allow_host_network: bool | None = None
    allow_host_pid: bool | None = None
    allow_host_path: bool | None = None
    require_run_as_non_root: bool | None = None
    require_read_only_root_filesystem: bool | None = None
    allowed_added_capabilities: tuple[str, ...] | None = None

    @field_validator("allowed_added_capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("allowed_added_capabilities must be a list")
        return tuple(str(item).upper() for item in value)


class ResourceConstraints(StrictModel):
    require_requests: bool | None = None
    require_limits: bool | None = None
    max_memory_limit_per_pod: str | None = None
    max_cpu_limit_per_pod: str | None = None
    max_sampled_memory_per_pod: str | None = None
    max_sampled_cpu_per_pod: str | None = None

    @field_validator(
        "max_memory_limit_per_pod",
        "max_cpu_limit_per_pod",
        "max_sampled_memory_per_pod",
        "max_sampled_cpu_per_pod",
    )
    @classmethod
    def validate_quantity(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                parse_quantity(value)
            except InvalidQuantity as exc:
                raise ValueError(str(exc)) from exc
        return value


class ReliabilityConstraints(StrictModel):
    minimum_replicas: int | None = Field(default=None, ge=1)
    max_pod_replacement_ready_seconds: int | None = Field(default=None, gt=0)


_DNS_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class NetworkingConstraints(StrictModel):
    allow_public_egress: bool | None = None
    allowed_external_domains: tuple[str, ...] | None = None

    @field_validator("allowed_external_domains", mode="before")
    @classmethod
    def normalize_domains(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("allowed_external_domains must be a list")
        normalized: list[str] = []
        for item in value:
            domain = str(item).strip().rstrip(".").lower()
            if not _DNS_NAME.fullmatch(domain):
                raise ValueError(f"invalid DNS name: {item!r}")
            normalized.append(domain)
        return tuple(normalized)


class ConstraintSet(StrictModel):
    security: SecurityConstraints = SecurityConstraints()
    resources: ResourceConstraints = ResourceConstraints()
    reliability: ReliabilityConstraints = ReliabilityConstraints()
    networking: NetworkingConstraints = NetworkingConstraints()


class CompanyProfile(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    constraints: ConstraintSet

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value != "0.1":
            raise ValueError("only profile schema_version '0.1' is supported")
        return value


def load_profile(path: Path) -> CompanyProfile:
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read profile {path}: {exc}") from exc
    if not isinstance(content, dict):
        raise ValueError("profile root must be a mapping")
    return CompanyProfile.model_validate(content)
