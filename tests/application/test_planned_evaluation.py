"""Review a plan once; measurements and AI commentary have distinct contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kubeproof.application.service import inspect_chart
from kubeproof.core.domain import (
    Assessment,
    EnvironmentInfo,
    ExecutionStatus,
)
from kubeproof.core.interpretation import AIInterpretation, InterpretationPoint
from kubeproof.core.plans import DnsTask, EvaluationPlan, PlanBudget
from kubeproof.core.profile import CompanyProfile
from kubeproof.evidence.bundle import verify_bundle
from kubeproof.execution.helm import HelmRenderRequest, HelmRenderResult
from kubeproof.execution.probes import PlannedExperiment
from kubeproof.execution.runtime import RuntimeResult


class Renderer:
    def render(self, request: HelmRenderRequest) -> HelmRenderResult:
        return HelmRenderResult(
            manifest=b"apiVersion: v1\nkind: ConfigMap\nmetadata: {name: example}\n", stderr=""
        )


def arguments(tmp_path: Path, profile: CompanyProfile) -> dict[str, Any]:
    return dict(
        chart="chart",
        version=None,
        profile=profile,
        values_files=(),
        set_values=(),
        output=tmp_path / "preview",
        renderer=Renderer(),
    )


def test_ai_plan_is_validated_and_sealed_without_executing(
    tmp_path: Path, strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    budget = PlanBudget(max_tasks=2, max_elapsed_seconds=30)

    def generate(context: dict[str, Any]) -> EvaluationPlan:
        calls.append(context)
        return EvaluationPlan(
            origin="ai",
            objective=context["objective"],
            budget=budget,
            tasks=(DnsTask(id="dns", rationale="inspect"),),
        )

    monkeypatch.setattr(
        "kubeproof.application.service.run_runtime",
        lambda **_: pytest.fail("preflight must not execute"),
    )
    result = inspect_chart(
        **arguments(tmp_path, strict_profile),
        plan_generator=generate,
        planning_objective="Inspect DNS.",
        plan_budget=budget,
    )
    assert len(calls) == 1
    assert result.execution_options.test_plan is not None
    assert result.execution_options.test_plan.origin == "ai"
    assert result.plan_execution is None
    assert verify_bundle(tmp_path / "preview").bundle_sha256
    saved = EvaluationPlan.model_validate_json(
        (tmp_path / "preview/artifacts/plan/plan.json").read_bytes()
    )
    assert saved == result.execution_options.test_plan


def test_ai_cannot_expand_confirmed_budget_or_execute_while_planning(
    tmp_path: Path, strict_profile: CompanyProfile
) -> None:
    def expand(context: dict[str, Any]) -> EvaluationPlan:
        return EvaluationPlan(
            origin="ai",
            objective=context["objective"],
            budget=PlanBudget(max_tasks=32),
            tasks=(DnsTask(id="dns", rationale="inspect"),),
        )

    with pytest.raises(ValueError, match="confirmed objective, budget"):
        inspect_chart(
            **arguments(tmp_path, strict_profile),
            plan_generator=expand,
            planning_objective="Inspect DNS.",
        )
    with pytest.raises(ValueError, match="separate static preflight"):
        inspect_chart(
            **arguments(tmp_path, strict_profile),
            plan_generator=expand,
            planning_objective="Inspect DNS.",
            execute_known_chart=True,
        )


@pytest.mark.parametrize("bad_reference", [False, True])
def test_ai_commentary_cannot_change_assessment_or_forge_evidence(
    tmp_path: Path,
    strict_profile: CompanyProfile,
    monkeypatch: pytest.MonkeyPatch,
    bad_reference: bool,
) -> None:
    archive = tmp_path / "chart.tgz"
    archive.write_bytes(b"chart")
    monkeypatch.setattr("kubeproof.application.service._chart_archive_version", lambda _: "1")
    frozen = EvaluationPlan(
        objective="Inspect DNS.", tasks=(DnsTask(id="dns", rationale="inspect"),)
    )

    def runtime(**kwargs: Any) -> RuntimeResult:
        experiment = kwargs["experiment"]
        assert isinstance(experiment, PlannedExperiment)
        result = experiment.not_run("Infrastructure was unavailable.", failed=True)
        return RuntimeResult(
            checks=result.checks,
            observations=(),
            findings=(),
            artifacts=result.artifacts,
            environment=EnvironmentInfo(
                provider="kind", cluster_name="test", cleanup_attempted=True, cleanup_succeeded=True
            ),
            plan_execution=experiment.execution,
        )

    monkeypatch.setattr("kubeproof.application.service.run_runtime", runtime)

    def interpret(context: dict[str, Any]) -> AIInterpretation:
        assert context["evaluation"]["execution_options"]["test_plan"] == frozen.model_dump(
            mode="json"
        )
        return AIInterpretation(
            points=(
                InterpretationPoint(
                    kind="hypothesis",
                    explanation="More evidence is required.",
                    check_ids=("imaginary-check" if bad_reference else "runtime.network",),
                ),
            )
        )

    args = arguments(tmp_path, strict_profile)
    args.update(
        chart=str(archive), execute_known_chart=True, test_plan=frozen, interpreter=interpret
    )
    result = inspect_chart(**args)
    check = next(item for item in result.checks if item.id == "runtime.network")
    assert check.assessment is Assessment.NOT_TESTED
    assert check.execution_status is ExecutionStatus.INFRASTRUCTURE_FAILURE
    assert not result.findings
    assert result.ai_interpretation is not None
    assert result.ai_interpretation.status == ("unavailable" if bad_reference else "completed")
    assert verify_bundle(tmp_path / "preview").bundle_sha256


def test_changed_plan_is_rejected_before_runtime(
    tmp_path: Path, strict_profile: CompanyProfile
) -> None:
    frozen = EvaluationPlan(
        objective="Inspect DNS.", tasks=(DnsTask(id="dns", rationale="inspect"),)
    )
    with pytest.raises(ValueError, match="plan changed"):
        inspect_chart(
            **arguments(tmp_path, strict_profile), test_plan=frozen, expected_plan_sha256="0" * 64
        )
