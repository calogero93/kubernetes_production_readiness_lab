"""Typed, reportable facts for the first KubeProof vertical slice."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceClass(StrEnum):
    STATIC_INPUT = "static_input"
    RUNTIME = "runtime"
    DOCUMENTATION = "documentation"


class Severity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    CANCELLED = "cancelled"


class Assessment(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    INCONCLUSIVE = "inconclusive"
    NOT_TESTED = "not_tested"
    NOT_APPLICABLE = "not_applicable"


class AdmissionOutcome(StrEnum):
    ADMIT = "admit"
    STATIC_ONLY = "static_only"
    REJECT_INVALID = "reject_invalid"


class ResourceRef(StrictModel):
    api_version: str = Field(description="Kubernetes API version of the referenced resource.")
    kind: str = Field(description="Kubernetes kind of the referenced resource.")
    namespace: str | None = Field(
        default=None,
        description="Kubernetes namespace of the resource, or null for cluster-scoped resources.",
    )
    name: str = Field(description="Kubernetes name of the referenced resource.")
    container: str | None = Field(
        default=None,
        description="Container name when the reference targets a specific container.",
    )


class ObservationProvenance(StrictModel):
    source_ref: str = Field(
        description="Input or runtime environment from which this fact was collected."
    )
    source_sha256: str | None = Field(
        default=None,
        description="SHA-256 digest of the source bytes, when those bytes are available.",
    )


class Observation(StrictModel):
    schema_version: Literal["1"] = Field(
        default="1", description="Version of the observation record schema."
    )
    id: str = Field(description="Unique identifier for this observed fact within the evaluation.")
    check_id: str = Field(description="Identifier of the check that produced this observation.")
    source_class: SourceClass = Field(
        description="Class of evidence from which the observation was obtained."
    )
    observation_type: str = Field(
        description="Stable machine-readable category describing the observed fact."
    )
    summary: str = Field(description="Human-readable summary of the observed fact.")
    resource: ResourceRef | None = Field(
        default=None,
        description="Kubernetes resource associated with the observation, when applicable.",
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured, observation-specific evidence and measurements.",
    )
    provenance: ObservationProvenance | None = Field(
        default=None,
        description="Source of this observation; required for schema 0.2 evaluations.",
    )


class Finding(StrictModel):
    id: str = Field(description="Unique identifier for this finding within the evaluation.")
    check_id: str = Field(description="Identifier of the check that produced this finding.")
    severity: Severity = Field(description="Impact level assigned to the finding.")
    title: str = Field(description="Short human-readable title of the finding.")
    description: str = Field(
        description="Detailed explanation of the condition and why it matters."
    )
    observation_ids: tuple[str, ...] = Field(
        description="Identifiers of the observations that provide evidence for this finding."
    )
    constraint: str | None = Field(
        default=None,
        description="Profile constraint evaluated or violated by this finding, when applicable.",
    )
    remediation: str | None = Field(
        default=None,
        description="Suggested action for addressing the finding, when available.",
    )
    limitation: str | None = Field(
        default=None,
        description="Known limitation or qualification affecting interpretation of the finding.",
    )

    @model_validator(mode="after")
    def requires_evidence(self) -> Finding:
        if not self.observation_ids:
            raise ValueError("a finding must reference at least one observation")
        return self


class CheckResult(StrictModel):
    id: str = Field(description="Stable machine-readable identifier of the check.")
    experiment_version: str = Field(
        default="1",
        description="Version of the deterministic check or experiment that produced this result.",
    )
    title: str = Field(description="Human-readable name of the check.")
    execution_status: ExecutionStatus = Field(
        description="Technical completion status of the check execution."
    )
    assessment: Assessment = Field(
        description="Conclusion reached by the check from the available evidence."
    )
    observation_ids: tuple[str, ...] = Field(
        default=(),
        description="Identifiers of observations produced or used by this check.",
    )
    finding_ids: tuple[str, ...] = Field(
        default=(),
        description="Identifiers of findings produced by this check.",
    )
    explanation: str | None = Field(
        default=None,
        description="Additional context for the execution status or assessment, when needed.",
    )


class AdmissionDecision(StrictModel):
    outcome: AdmissionOutcome = Field(
        description="Decision controlling whether runtime evaluation may proceed."
    )
    matched_rule_ids: tuple[str, ...] = Field(
        default=(),
        description="Identifiers of safety rules that contributed to the admission decision.",
    )
    explanation: str = Field(description="Human-readable rationale for the admission decision.")


class InputIdentity(StrictModel):
    chart: str = Field(description="Chart reference or path supplied for evaluation.")
    requested_version: str | None = Field(
        default=None,
        description="Chart version requested by the user, when explicitly specified.",
    )
    resolved_version: str | None = Field(
        default=None,
        description="Concrete chart version resolved and evaluated, when known.",
    )
    chart_package_sha256: str | None = Field(
        default=None,
        description="SHA-256 digest of the evaluated chart package, when available.",
    )
    rendered_manifest_sha256: str = Field(
        description="SHA-256 digest of the complete rendered Kubernetes manifest."
    )
    values_sha256: tuple[str, ...] = Field(
        default=(),
        description="Ordered SHA-256 digests of the values files used during rendering.",
    )
    set_values_sha256: tuple[str, ...] = Field(
        default=(),
        description="Ordered SHA-256 digests of Helm --set assignments; values are not disclosed.",
    )
    profile_sha256: str | None = Field(
        default=None,
        description="SHA-256 digest of the canonical normalized profile snapshot.",
    )


class ExecutionOptions(StrictModel):
    release_name: str = Field(description="Helm release name used to render or install the chart.")
    namespace: str = Field(description="Kubernetes namespace targeted by the evaluation.")
    kubernetes_version: str | None = Field(
        default=None,
        description="Kubernetes version requested for rendering or runtime evaluation.",
    )
    render_timeout_seconds: int = Field(
        description="Maximum number of seconds allowed for chart rendering."
    )
    execute_known_chart: bool = Field(
        default=False,
        description="Whether runtime evaluation of an explicitly trusted chart was requested.",
    )
    install_timeout_seconds: int | None = Field(
        default=None,
        description="Maximum number of seconds allowed for runtime chart installation.",
    )
    steady_state_seconds: int | None = Field(
        default=None,
        description="Observation window in seconds after the workload reaches steady state.",
    )
    max_recovery_targets: int | None = Field(
        default=None,
        description="Maximum number of workload targets selected for recovery testing.",
    )


class EnvironmentInfo(StrictModel):
    provider: str = Field(description="Provider used to create the runtime test environment.")
    cluster_name: str = Field(description="Name of the cluster used for runtime evaluation.")
    cleanup_attempted: bool = Field(
        description="Whether cleanup of the runtime environment was attempted."
    )
    cleanup_succeeded: bool = Field(
        description="Whether cleanup of the runtime environment completed successfully."
    )
    cleanup_error: str | None = Field(
        default=None,
        description="Error reported during environment cleanup, if cleanup failed.",
    )
    leftovers_before_cluster_deletion: tuple[str, ...] = Field(
        default=(),
        description="Resources still present immediately before the test cluster was deleted.",
    )
    runtime_safety_rule_ids: tuple[str, ...] = Field(
        default=(),
        description="Identifiers of safety rules triggered while monitoring runtime execution.",
    )
    kubernetes_server_version: str | None = Field(
        default=None,
        description="Version reported by the disposable cluster Kubernetes API, when available.",
    )


class ToolFingerprint(StrictModel):
    python_version: str = Field(description="Python interpreter version used for the evaluation.")
    platform: str = Field(description="Operating system and machine architecture of the runner.")
    helm_version: str | None = Field(
        default=None, description="Helm CLI version, or null when it could not be obtained."
    )
    kind_version: str | None = Field(
        default=None, description="kind CLI version, or null when runtime did not use kind."
    )


class EvaluationResult(StrictModel):
    schema_version: Literal["0.1", "0.2"] = Field(
        default="0.1",
        description="Version of the serialized evaluation result schema.",
    )
    evaluation_id: str = Field(description="Unique identifier for this evaluation run.")
    generated_at: datetime = Field(
        description="Timestamp at which the evaluation result was generated."
    )
    tool_version: str = Field(description="KubeProof version that generated this result.")
    profile_name: str = Field(description="Name of the constraint profile used for evaluation.")
    input: InputIdentity = Field(
        description="Identity and content digests of the evaluated chart input."
    )
    execution_options: ExecutionOptions = Field(
        description="Options that controlled rendering and runtime evaluation."
    )
    admission: AdmissionDecision = Field(
        description="Safety decision governing whether runtime evaluation was allowed."
    )
    checks: tuple[CheckResult, ...] = Field(
        description="Results of all checks included in the evaluation."
    )
    observations: tuple[Observation, ...] = Field(
        description="Evidence collected by static and runtime checks."
    )
    findings: tuple[Finding, ...] = Field(
        description="Actionable conclusions derived from the collected observations."
    )
    environment: EnvironmentInfo | None = Field(
        default=None,
        description="Runtime environment details, or null when no runtime evaluation occurred.",
    )
    tool_fingerprint: ToolFingerprint | None = Field(
        default=None,
        description="Tool versions and platform details for interpreting the evaluation.",
    )

    @model_validator(mode="after")
    def references_are_consistent(self) -> EvaluationResult:
        observations = {item.id for item in self.observations}
        findings = {item.id for item in self.findings}
        for finding in self.findings:
            missing = set(finding.observation_ids) - observations
            if missing:
                raise ValueError(f"finding {finding.id} references missing observations: {missing}")
        for check in self.checks:
            missing_observations = set(check.observation_ids) - observations
            missing_findings = set(check.finding_ids) - findings
            if missing_observations or missing_findings:
                raise ValueError(f"check {check.id} contains invalid references")
        if self.schema_version == "0.2":
            check_ids = {item.id for item in self.checks}
            if (
                len(observations) != len(self.observations)
                or len(findings) != len(self.findings)
                or len(check_ids) != len(self.checks)
            ):
                raise ValueError("evaluation contains duplicate check, observation or finding ids")
            if self.tool_fingerprint is None or self.input.profile_sha256 is None:
                raise ValueError("schema 0.2 requires tool and profile fingerprints")
            for observation in self.observations:
                if observation.check_id not in check_ids or observation.provenance is None:
                    raise ValueError(f"observation {observation.id} has no check or provenance")
                expected_source = (
                    SourceClass.STATIC_INPUT
                    if observation.check_id.startswith("static.")
                    else SourceClass.RUNTIME
                    if observation.check_id.startswith("runtime.")
                    else SourceClass.DOCUMENTATION
                    if observation.check_id.startswith("documentation.")
                    else None
                )
                if expected_source is None or observation.source_class is not expected_source:
                    raise ValueError(f"observation {observation.id} has mismatched source class")
            for finding in self.findings:
                if finding.check_id not in check_ids:
                    raise ValueError(f"finding {finding.id} references a missing check")
        return self
