"""Offline comparison of two sealed evaluations under documented tolerances."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from kubeproof.core.domain import EvaluationResult, Observation
from kubeproof.core.profile import CompanyProfile
from kubeproof.core.quantities import parse_quantity
from kubeproof.evidence.bundle import canonical_json_bytes


@dataclass(frozen=True)
class RepeatabilityResult:
    differences: tuple[str, ...]
    limitations: tuple[str, ...]

    @property
    def status(self) -> str:
        if self.differences:
            return "different"
        return "inconclusive" if self.limitations else "repeatable"


def _number(value: object, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label} measurement: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"invalid {label} measurement: {value!r}")
    return number


def _runtime_checks(evaluation: EvaluationResult) -> dict[str, tuple[str, str, str]]:
    return {
        check.id: (check.experiment_version, check.execution_status, check.assessment)
        for check in evaluation.checks
        if check.id.startswith("runtime.")
    }


def _runtime_findings(evaluation: EvaluationResult) -> list[tuple[str, str, str, str | None]]:
    return sorted(
        (
            (item.check_id, item.severity, item.title, item.constraint)
            for item in evaluation.findings
            if item.check_id.startswith("runtime.")
        ),
        key=lambda item: (item[0], item[1], item[2], item[3] or ""),
    )


def _observations(evaluation: EvaluationResult, observation_type: str) -> list[Observation]:
    return [item for item in evaluation.observations if item.observation_type == observation_type]


def _metric_peaks(
    evaluation: EvaluationResult,
) -> dict[tuple[str, tuple[str, ...]], tuple[Decimal, Decimal]]:
    peaks: dict[tuple[str, tuple[str, ...]], tuple[Decimal, Decimal]] = {}
    for sample in _observations(evaluation, "resources.pod_metrics_sample"):
        if sample.data.get("complete") is not True:
            continue
        if sample.resource is None or not isinstance(sample.data.get("containers_expected"), list):
            raise ValueError(f"metric sample {sample.id} has no comparable workload identity")
        key = (
            sample.resource.namespace or "",
            tuple(sorted(str(item) for item in sample.data["containers_expected"])),
        )
        cpu = _number(sample.data.get("cpu_base_units"), "CPU")
        memory = _number(sample.data.get("memory_base_units"), "memory")
        previous = peaks.get(key, (Decimal(0), Decimal(0)))
        peaks[key] = (max(previous[0], cpu), max(previous[1], memory))
    return peaks


def _duration_by_resource(evaluation: EvaluationResult) -> dict[tuple[str, str], Decimal | None]:
    durations: dict[tuple[str, str], Decimal | None] = {}
    for sample in _observations(evaluation, "recovery.pod_replacement"):
        if sample.resource is None:
            raise ValueError(f"recovery observation {sample.id} has no resource")
        key = (sample.resource.namespace or "", sample.resource.name)
        if key in durations:
            raise ValueError(f"duplicate recovery target: {key}")
        value = sample.data.get("ready_seconds")
        durations[key] = None if value is None else _number(value, "recovery duration")
    return durations


def _installation_duration(evaluation: EvaluationResult) -> Decimal | None:
    observations = _observations(evaluation, "installation.helm_and_readiness")
    if len(observations) > 1:
        raise ValueError("evaluation has multiple installation duration observations")
    if not observations:
        return None
    return _number(observations[0].data.get("duration_seconds"), "installation duration")


def _within_duration_tolerance(first: Decimal, second: Decimal) -> bool:
    return abs(first - second) <= max(Decimal(5), first * Decimal("0.20"))


def cpu_peak_tolerance(first: Decimal, second: Decimal, profile_limit: Decimal) -> Decimal:
    """25% of the larger peak, with a bounded policy-relative low-load floor."""
    if min(first, second, profile_limit) < 0:
        raise ValueError("CPU peaks and profile limit must be non-negative")
    relative = max(first, second) * Decimal("0.25")
    absolute = min(Decimal("0.020"), profile_limit * Decimal("0.01"))
    return max(relative, absolute)


def compare_evaluations(
    first: EvaluationResult, second: EvaluationResult, profile: CompanyProfile
) -> RepeatabilityResult:
    """Compare evidence repeatability without changing product check assessments."""
    if first.schema_version != "0.2" or second.schema_version != "0.2":
        raise ValueError("repeatability comparison requires schema 0.2 bundles")
    identity_fields = (
        "requested_version",
        "resolved_version",
        "chart_package_sha256",
        "rendered_manifest_sha256",
        "values_sha256",
        "set_values_sha256",
        "profile_sha256",
    )
    if any(
        getattr(first.input, field) != getattr(second.input, field) for field in identity_fields
    ):
        raise ValueError("evaluations have different chart, values or profile identities")
    if (
        first.execution_options != second.execution_options
        or first.profile_name != second.profile_name
    ):
        raise ValueError("evaluations used different profiles or execution options")
    if profile.name != first.profile_name:
        raise ValueError("comparison profile does not match the evaluations")
    profile_digest = hashlib.sha256(
        canonical_json_bytes(profile.model_dump(mode="json"))
    ).hexdigest()
    if profile_digest != first.input.profile_sha256:
        raise ValueError("comparison profile content does not match the evaluations")

    differences: list[str] = []
    limitations: list[str] = []
    if first.tool_fingerprint != second.tool_fingerprint:
        limitations.append("runner or tool fingerprints differ")
    first_environment = first.environment
    second_environment = second.environment
    if (first_environment is None) != (second_environment is None):
        differences.append("one run omitted runtime execution")
    elif (
        first_environment is not None
        and second_environment is not None
        and (
            first_environment.kubernetes_server_version
            != second_environment.kubernetes_server_version
            or first_environment.metrics_server_manifest_sha256
            != second_environment.metrics_server_manifest_sha256
        )
    ):
        limitations.append("runtime environment fingerprints differ")

    if tuple(c for c in first.checks if c.id.startswith("static.")) != tuple(
        c for c in second.checks if c.id.startswith("static.")
    ):
        differences.append("static checks differ")
    if tuple(o for o in first.observations if o.check_id.startswith("static.")) != tuple(
        o for o in second.observations if o.check_id.startswith("static.")
    ):
        differences.append("static observations differ")
    if tuple(f for f in first.findings if f.check_id.startswith("static.")) != tuple(
        f for f in second.findings if f.check_id.startswith("static.")
    ):
        differences.append("static findings differ")
    if first.admission != second.admission:
        differences.append("admission decisions differ")
    if _runtime_checks(first) != _runtime_checks(second):
        differences.append("runtime check execution or assessments differ")
    if _runtime_findings(first) != _runtime_findings(second):
        differences.append("runtime finding classifications differ")

    first_install = _installation_duration(first)
    second_install = _installation_duration(second)
    if (first_install is None) != (second_install is None):
        differences.append("installation duration is missing from one run")
    elif (
        first_install is not None
        and second_install is not None
        and not _within_duration_tolerance(first_install, second_install)
    ):
        differences.append("installation durations exceed 5 seconds / 20% tolerance")

    first_recovery = _duration_by_resource(first)
    second_recovery = _duration_by_resource(second)
    if first_recovery.keys() != second_recovery.keys():
        differences.append("recovery targets differ")
    for recovery_key in sorted(first_recovery.keys() & second_recovery.keys()):
        left, right = first_recovery[recovery_key], second_recovery[recovery_key]
        if (left is None) != (right is None) or (
            left is not None and right is not None and not _within_duration_tolerance(left, right)
        ):
            differences.append(f"recovery duration differs for {recovery_key[0]}/{recovery_key[1]}")

    first_peaks = _metric_peaks(first)
    second_peaks = _metric_peaks(second)
    if (
        first_environment is not None
        and second_environment is not None
        and (not first_peaks or not second_peaks)
    ):
        limitations.append("one or both runs lack complete CPU/memory samples")
    if any(
        item.data.get("complete") is not True
        for evaluation in (first, second)
        for item in _observations(evaluation, "resources.pod_metrics_sample")
    ):
        limitations.append("one or both runs include incomplete CPU/memory samples")
    if first_peaks.keys() != second_peaks.keys():
        limitations.append("complete CPU/memory samples cover different workload groups")
    cpu_limit = profile.constraints.resources.max_sampled_cpu_per_pod
    if (first_peaks or second_peaks) and cpu_limit is None:
        limitations.append("CPU numeric comparison needs a profile CPU limit")
    for metric_key in sorted(first_peaks.keys() & second_peaks.keys()):
        first_cpu, first_memory = first_peaks[metric_key]
        second_cpu, second_memory = second_peaks[metric_key]
        name = f"{metric_key[0]}/" + ",".join(metric_key[1])
        if cpu_limit is not None and abs(first_cpu - second_cpu) > cpu_peak_tolerance(
            first_cpu, second_cpu, parse_quantity(cpu_limit)
        ):
            differences.append(f"sampled CPU peaks exceed tolerance for {name}")
        if abs(first_memory - second_memory) > max(first_memory, second_memory) * Decimal("0.25"):
            differences.append(f"sampled memory peaks exceed 25% tolerance for {name}")

    first_dns = {
        item.data.get("domain") for item in _observations(first, "network.external_dns_query")
    }
    second_dns = {
        item.data.get("domain") for item in _observations(second, "network.external_dns_query")
    }
    if first_dns != second_dns:
        differences.append("observed external DNS domain sets differ")
    return RepeatabilityResult(tuple(differences), tuple(limitations))
