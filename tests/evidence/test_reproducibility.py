from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from kubeproof.core.domain import (
    AdmissionDecision,
    AdmissionOutcome,
    Assessment,
    CheckResult,
    EnvironmentInfo,
    EvaluationResult,
    ExecutionOptions,
    ExecutionStatus,
    InputIdentity,
    Observation,
    ObservationProvenance,
    ResourceRef,
    SourceClass,
    ToolFingerprint,
)
from kubeproof.core.profile import CompanyProfile
from kubeproof.evidence.bundle import canonical_json_bytes, write_bundle
from kubeproof.evidence.reproducibility import compare_evaluations, cpu_peak_tolerance
from kubeproof.interfaces.cli import app


def _evaluation(
    profile: CompanyProfile,
    *,
    cpu: str,
    cluster: str,
    assessment: Assessment = Assessment.PASS,
    complete: bool = True,
) -> EvaluationResult:
    profile_sha256 = hashlib.sha256(
        canonical_json_bytes(profile.model_dump(mode="json"))
    ).hexdigest()
    return EvaluationResult(
        schema_version="0.2",
        evaluation_id=cluster,
        generated_at=datetime(2026, 9, 23, tzinfo=UTC),
        tool_version="0.1.0a0",
        profile_name=profile.name,
        input=InputIdentity(
            chart="example/chart",
            rendered_manifest_sha256="a" * 64,
            profile_sha256=profile_sha256,
        ),
        execution_options=ExecutionOptions(
            release_name="target",
            namespace="product",
            render_timeout_seconds=90,
            execute_known_chart=True,
        ),
        admission=AdmissionDecision(
            outcome=AdmissionOutcome.ADMIT,
            explanation="admitted",
        ),
        checks=(
            CheckResult(
                id="runtime.resources",
                title="Observed resource consumption",
                execution_status=ExecutionStatus.COMPLETED,
                assessment=assessment,
                observation_ids=("sample-1",),
            ),
        ),
        observations=(
            Observation(
                id="sample-1",
                check_id="runtime.resources",
                source_class=SourceClass.RUNTIME,
                observation_type="resources.pod_metrics_sample",
                summary="Complete CPU and memory sample.",
                resource=ResourceRef(
                    api_version="v1",
                    kind="Pod",
                    namespace="product",
                    name=f"product-{cluster}",
                ),
                data={
                    "complete": complete,
                    "containers_expected": ["controller"],
                    "cpu_base_units": cpu,
                    "memory_base_units": "24000000",
                },
                provenance=ObservationProvenance(source_ref=f"environment:kind:{cluster}"),
            ),
        ),
        findings=(),
        environment=EnvironmentInfo(
            provider="kind",
            cluster_name=cluster,
            cleanup_attempted=True,
            cleanup_succeeded=True,
            kubernetes_server_version="v1.30.0",
            metrics_server_manifest_sha256="b" * 64,
        ),
        tool_fingerprint=ToolFingerprint(
            python_version="3.12.3",
            platform="linux/x86_64",
            helm_version="v3.21.1",
            kind_version="v0.23.0",
        ),
    )


def test_low_cpu_variation_within_policy_relative_floor(
    strict_profile: CompanyProfile,
) -> None:
    first = _evaluation(strict_profile, cpu="0.010879829", cluster="first")
    second = _evaluation(strict_profile, cpu="0.000531004", cluster="second")

    result = compare_evaluations(first, second, strict_profile)

    assert result.status == "repeatable"
    assert cpu_peak_tolerance(
        Decimal("0.010879829"), Decimal("0.000531004"), Decimal("2")
    ) == Decimal("0.020")


def test_material_cpu_difference_exceeds_tolerance(strict_profile: CompanyProfile) -> None:
    first = _evaluation(strict_profile, cpu="1.0", cluster="first")
    second = _evaluation(strict_profile, cpu="1.5", cluster="second")

    result = compare_evaluations(first, second, strict_profile)

    assert result.status == "different"
    assert any("sampled CPU peaks" in item for item in result.differences)


def test_assessment_difference_is_never_excused_by_cpu_tolerance(
    strict_profile: CompanyProfile,
) -> None:
    first = _evaluation(strict_profile, cpu="0.010", cluster="first")
    second = _evaluation(
        strict_profile,
        cpu="0.011",
        cluster="second",
        assessment=Assessment.FAIL,
    )

    result = compare_evaluations(first, second, strict_profile)

    assert result.status == "different"
    assert "runtime check execution or assessments differ" in result.differences


def test_absolute_cpu_floor_is_capped_at_twenty_millicores() -> None:
    assert cpu_peak_tolerance(Decimal("0.001"), Decimal("0.002"), Decimal("100")) == Decimal(
        "0.020"
    )
    assert cpu_peak_tolerance(Decimal("0.001"), Decimal("0.002"), Decimal("0.1")) == Decimal(
        "0.001"
    )


def test_cpu_comparison_without_profile_limit_is_inconclusive(
    strict_profile: CompanyProfile,
) -> None:
    payload = strict_profile.model_dump(mode="json")
    payload["constraints"]["resources"]["max_sampled_cpu_per_pod"] = None
    profile = CompanyProfile.model_validate(payload)

    result = compare_evaluations(
        _evaluation(profile, cpu="0.010", cluster="first"),
        _evaluation(profile, cpu="0.011", cluster="second"),
        profile,
    )

    assert result.status == "inconclusive"
    assert "CPU numeric comparison needs a profile CPU limit" in result.limitations


def test_incomplete_metric_samples_cannot_prove_numeric_repeatability(
    strict_profile: CompanyProfile,
) -> None:
    result = compare_evaluations(
        _evaluation(strict_profile, cpu="0.010", cluster="first", complete=False),
        _evaluation(strict_profile, cpu="0.011", cluster="second", complete=False),
        strict_profile,
    )

    assert result.status == "inconclusive"
    assert "one or both runs lack complete CPU/memory samples" in result.limitations


def test_compare_cli_verifies_bundles_and_reports_repeatability(
    tmp_path: Path, strict_profile: CompanyProfile
) -> None:
    for name in ("first", "second"):
        write_bundle(
            tmp_path / name,
            _evaluation(strict_profile, cpu="0.010", cluster=name),
            normalized_profile=strict_profile.model_dump(mode="json"),
            redacted_manifest="---\n",
        )

    result = CliRunner().invoke(app, ["compare", str(tmp_path / "first"), str(tmp_path / "second")])

    assert result.exit_code == 0
    assert "Repeatability: repeatable" in result.stdout
