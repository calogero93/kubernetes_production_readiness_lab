"""Application use case for a static Helm qualification."""

from __future__ import annotations

import hashlib
import tarfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from kubeproof import __version__
from kubeproof.core.analysis import analyze
from kubeproof.core.domain import (
    AdmissionDecision,
    AdmissionOutcome,
    Assessment,
    CheckResult,
    EvaluationResult,
    ExecutionOptions,
    ExecutionStatus,
    HttpProbeOptions,
    InputIdentity,
    Observation,
    ObservationProvenance,
    OperatorApproval,
    SourceClass,
)
from kubeproof.core.interpretation import AIInterpretation
from kubeproof.core.manifests import parse_manifests, redact_manifest
from kubeproof.core.plans import EvaluationPlan, HttpTask, PlanBudget, default_plan
from kubeproof.core.profile import CompanyProfile
from kubeproof.core.safety import (
    OVERRIDABLE_HOST_RULES,
    approval_scope_sha256,
    decide_local_admission,
)
from kubeproof.evidence.bundle import BundleError, canonical_json_bytes, write_bundle
from kubeproof.execution.fingerprint import capture_tool_fingerprint
from kubeproof.execution.helm import HelmRenderer, HelmRenderRequest, HelmRenderResult
from kubeproof.execution.probes import CATALOG, PlannedExperiment, catalog_context
from kubeproof.execution.runtime import RuntimeExperiment, run_runtime


class ManifestRenderer(Protocol):
    def render(self, request: HelmRenderRequest) -> HelmRenderResult: ...


@runtime_checkable
class BaselinePlanBuilder(Protocol):
    def baseline_plan(
        self,
        resources: tuple[dict[str, Any], ...],
        render: HelmRenderRequest,
        profile: CompanyProfile,
    ) -> EvaluationPlan: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chart_archive_version(path: Path) -> str | None:
    if not path.is_file() or path.suffix != ".tgz":
        return None
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            member = next(
                (
                    item
                    for item in archive
                    if item.name.count("/") == 1 and item.name.endswith("/Chart.yaml")
                ),
                None,
            )
            if member is None or member.size > 64_000:
                raise ValueError("chart archive has no bounded Chart.yaml")
            content = archive.extractfile(member)
            if content is None:
                raise ValueError("chart archive metadata cannot be read")
            metadata = yaml.safe_load(content.read(64_001))
    except (OSError, tarfile.TarError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read chart archive metadata: {exc}") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("version"), str):
        raise ValueError("chart archive has no version in Chart.yaml")
    return str(metadata["version"])


def _with_provenance(
    observation: Observation, manifest_sha256: str, runtime_cluster: str | None
) -> Observation:
    if observation.source_class is SourceClass.STATIC_INPUT:
        provenance = ObservationProvenance(
            source_ref="input:rendered-manifest", source_sha256=manifest_sha256
        )
    elif observation.source_class is SourceClass.RUNTIME and runtime_cluster is not None:
        provenance = ObservationProvenance(source_ref=f"environment:kind:{runtime_cluster}")
    else:
        raise ValueError(f"no provenance source for observation {observation.id}")
    return Observation.model_validate(
        {**observation.model_dump(mode="python"), "provenance": provenance}
    )


def inspect_chart(
    *,
    chart: str,
    version: str | None,
    profile: CompanyProfile,
    values_files: tuple[Path, ...],
    set_values: tuple[str, ...],
    output: Path,
    release_name: str = "kubeproof-target",
    namespace: str = "kubeproof-product",
    kubernetes_version: str | None = None,
    render_timeout_seconds: int = 90,
    renderer: ManifestRenderer | None = None,
    execute_known_chart: bool = False,
    install_timeout_seconds: int = 300,
    steady_state_seconds: int = 60,
    max_recovery_targets: int = 5,
    experiment: RuntimeExperiment | BaselinePlanBuilder | None = None,
    operator_approval: OperatorApproval | None = None,
    expected_rendered_manifest_sha256: str | None = None,
    http_probe: HttpProbeOptions | None = None,
    test_plan: EvaluationPlan | None = None,
    expected_plan_sha256: str | None = None,
    plan_generator: Callable[[dict[str, Any]], EvaluationPlan] | None = None,
    planning_objective: str | None = None,
    plan_budget: PlanBudget | None = None,
    interpreter: Callable[[dict[str, Any]], AIInterpretation] | None = None,
) -> EvaluationResult:
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise BundleError(f"output directory must be new or empty: {output}")
    if (
        experiment is not None
        and not execute_known_chart
        and not hasattr(experiment, "baseline_plan")
    ):
        raise ValueError("runtime experiments require execute_known_chart=True")
    if experiment is not None and (
        http_probe is not None or test_plan is not None or plan_generator is not None
    ):
        raise ValueError("legacy experiments cannot be combined with baseline plan options")
    if plan_generator is not None and (
        execute_known_chart or test_plan is not None or http_probe is not None
    ):
        raise ValueError(
            "AI planning requires a separate static preflight; execute the reviewed plan afterward"
        )
    if plan_generator is not None and not planning_objective:
        raise ValueError("AI planning requires a confirmed objective")
    if execute_known_chart and (not Path(chart).is_file() or Path(chart).suffix != ".tgz"):
        raise ValueError("runtime execution requires a local pinned .tgz chart archive")
    if Path(chart).is_file() and Path(chart).stat().st_size > 100 * 1024 * 1024:
        raise ValueError("chart archive exceeds the 100 MiB input limit")
    helm = renderer or HelmRenderer()
    request = HelmRenderRequest(
        chart=chart,
        release_name=release_name,
        namespace=namespace,
        version=version,
        values_files=values_files,
        set_values=set_values,
        kubernetes_version=kubernetes_version,
        timeout_seconds=render_timeout_seconds,
    )
    render = helm.render(request)
    if (
        expected_rendered_manifest_sha256 is not None
        and hashlib.sha256(render.manifest).hexdigest() != expected_rendered_manifest_sha256
    ):
        raise ValueError("rendered manifest changed after operator review")
    resources = parse_manifests(render.manifest)
    analysis = analyze(resources, profile)
    if isinstance(experiment, BaselinePlanBuilder):
        test_plan = experiment.baseline_plan(resources, request, profile)
        experiment = None
    if experiment is None:
        if plan_generator is not None:
            confirmed_budget = plan_budget or PlanBudget()
            context = {
                "objective": planning_objective,
                "budget": confirmed_budget.model_dump(mode="json"),
                "profile": profile.model_dump(mode="json"),
                "rendered_manifest": redact_manifest(resources),
                "static_checks": [check.model_dump(mode="json") for check in analysis.checks],
                "static_findings": [
                    finding.model_dump(mode="json") for finding in analysis.findings
                ],
                "catalog": catalog_context(),
            }
            if len(canonical_json_bytes(context)) > 1_000_000:
                raise ValueError("AI planning context exceeds the 1 MiB limit")
            test_plan = plan_generator(context)
            if (
                test_plan.origin != "ai"
                or test_plan.budget != confirmed_budget
                or test_plan.objective != planning_objective
            ):
                raise ValueError("AI plan changed the confirmed objective, budget or origin")
        test_plan = test_plan or default_plan(
            http=http_probe,
            steady_state_seconds=steady_state_seconds,
            max_recovery_targets=max_recovery_targets,
        )
        if http_probe is not None and not any(
            isinstance(task, HttpTask) and task.parameters == http_probe for task in test_plan.tasks
        ):
            raise ValueError("HTTP shorthand differs from the reviewed plan")
        if expected_plan_sha256 is not None and test_plan.digest() != expected_plan_sha256:
            raise ValueError("plan changed after operator review")
        experiment = PlannedExperiment(test_plan, profile)
    experiment.validate_resources(resources, request)
    admission = decide_local_admission(analysis.observations)
    normalized_profile = profile.model_dump(mode="json")
    profile_sha256 = hashlib.sha256(canonical_json_bytes(normalized_profile)).hexdigest()
    manifest_sha256 = hashlib.sha256(render.manifest).hexdigest()
    chart_path = Path(chart)
    chart_sha256 = _sha256_file(chart_path) if chart_path.is_file() else None
    values_sha256 = tuple(_sha256_file(path) for path in values_files)
    set_values_sha256 = tuple(hashlib.sha256(value.encode()).hexdigest() for value in set_values)
    host_rules = frozenset(admission.matched_rule_ids)
    scope = (
        approval_scope_sha256(
            chart_sha256=chart_sha256,
            rendered_manifest_sha256=manifest_sha256,
            profile_sha256=profile_sha256,
            values_sha256=values_sha256,
            set_values_sha256=set_values_sha256,
            release_name=release_name,
            namespace=namespace,
            matched_rule_ids=admission.matched_rule_ids,
            http_probe=http_probe,
            test_plan=test_plan,
        )
        if chart_sha256 is not None
        and chart_path.suffix == ".tgz"
        and host_rules
        and host_rules <= OVERRIDABLE_HOST_RULES
        else None
    )
    if operator_approval is not None:
        if not execute_known_chart or scope is None:
            raise ValueError("operator approval applies only to an executable host-access chart")
        if operator_approval.scope_sha256 != scope:
            raise ValueError(
                "operator approval does not match the rendered chart, values and profile"
            )
        admission = AdmissionDecision(
            outcome=AdmissionOutcome.ADMIT_WITH_OPERATOR_APPROVAL,
            matched_rule_ids=admission.matched_rule_ids,
            approval_scope_sha256=scope,
            operator_approval=operator_approval,
            explanation=(
                "Local host-access rules were disclosed and explicitly accepted for this "
                "input by a self-declared operator. kind is not hostile-code isolation."
            ),
        )
    elif scope is not None:
        admission = admission.model_copy(update={"approval_scope_sha256": scope})
    runtime = (
        run_runtime(
            request=request,
            resources=resources,
            profile=profile,
            install_timeout_seconds=install_timeout_seconds,
            steady_state_seconds=steady_state_seconds,
            max_recovery_targets=max_recovery_targets,
            experiment=experiment,
            approved_host_rule_ids=(
                admission.matched_rule_ids
                if admission.outcome is AdmissionOutcome.ADMIT_WITH_OPERATOR_APPROVAL
                else ()
            ),
        )
        if execute_known_chart
        and admission.outcome
        in {AdmissionOutcome.ADMIT, AdmissionOutcome.ADMIT_WITH_OPERATOR_APPROVAL}
        else None
    )
    static_checks = tuple(check for check in analysis.checks if not check.id.startswith("runtime."))
    untested = tuple(check for check in analysis.checks if check.id.startswith("runtime."))
    if isinstance(experiment, PlannedExperiment):
        selected = {experiment.check_for(task) for task in experiment.plan.tasks}
        untested = tuple(check for check in untested if check.id not in selected)
        untested += tuple(
            CheckResult(
                id=experiment.check_for(task),
                title=CATALOG[task.capability].title,
                execution_status=ExecutionStatus.COMPLETED,
                assessment=Assessment.NOT_TESTED,
                explanation="Frozen plan was reviewed; runtime execution was not requested.",
            )
            for task in experiment.plan.tasks
        )
    if admission.outcome is AdmissionOutcome.STATIC_ONLY:
        rules = ", ".join(admission.matched_rule_ids)
        untested = tuple(
            check.model_copy(
                update={"explanation": f"Local safety admission stopped execution: {rules}"}
            )
            for check in untested
        )
    observations = tuple(
        _with_provenance(
            observation,
            manifest_sha256,
            runtime.environment.cluster_name if runtime is not None else None,
        )
        for observation in analysis.observations + (runtime.observations if runtime else ())
    )
    evaluation = EvaluationResult(
        schema_version="0.2",
        evaluation_id=str(uuid.uuid4()),
        generated_at=datetime.now(UTC),
        tool_version=__version__,
        profile_name=profile.name,
        input=InputIdentity(
            chart=chart,
            requested_version=version,
            resolved_version=_chart_archive_version(Path(chart)),
            chart_package_sha256=chart_sha256,
            rendered_manifest_sha256=manifest_sha256,
            values_sha256=values_sha256,
            set_values_sha256=set_values_sha256,
            profile_sha256=profile_sha256,
        ),
        execution_options=ExecutionOptions(
            release_name=release_name,
            namespace=namespace,
            kubernetes_version=kubernetes_version,
            render_timeout_seconds=render_timeout_seconds,
            execute_known_chart=execute_known_chart,
            install_timeout_seconds=install_timeout_seconds if execute_known_chart else None,
            steady_state_seconds=steady_state_seconds if execute_known_chart else None,
            max_recovery_targets=max_recovery_targets if execute_known_chart else None,
            http_probe=http_probe,
            test_plan=test_plan,
        ),
        admission=admission,
        checks=static_checks + (runtime.checks if runtime else untested),
        observations=observations,
        findings=analysis.findings + (runtime.findings if runtime else ()),
        environment=runtime.environment if runtime else None,
        plan_execution=runtime.plan_execution if runtime else None,
        tool_fingerprint=capture_tool_fingerprint(
            helm_executable=helm._executable if isinstance(helm, HelmRenderer) else None,
            used_kind=runtime is not None,
        ),
    )
    artifacts = dict(runtime.artifacts) if runtime else {}
    if test_plan is not None:
        artifacts["artifacts/plan/plan.json"] = test_plan.model_dump_json(indent=2) + "\n"
    if interpreter is not None and execute_known_chart:
        try:
            interpretation_context = {
                "evaluation": evaluation.model_dump(mode="json"),
                "redacted_manifest": redact_manifest(resources),
            }
            if len(canonical_json_bytes(interpretation_context)) > 1_000_000:
                raise ValueError("AI interpretation context exceeds the 1 MiB limit")
            interpretation = interpreter(interpretation_context)
            evaluation = EvaluationResult.model_validate(
                {**evaluation.model_dump(mode="python"), "ai_interpretation": interpretation}
            )
        except Exception as exc:
            evaluation = evaluation.model_copy(
                update={
                    "ai_interpretation": AIInterpretation(
                        status="unavailable",
                        error=str(exc),
                        limitations=(
                            "AI commentary is unavailable; deterministic results are unchanged.",
                        ),
                    )
                }
            )
        assert evaluation.ai_interpretation is not None
        artifacts["artifacts/ai/interpretation.json"] = (
            evaluation.ai_interpretation.model_dump_json(indent=2) + "\n"
        )
    write_bundle(
        output,
        evaluation,
        normalized_profile=normalized_profile,
        redacted_manifest=redact_manifest(resources),
        artifacts=artifacts or None,
    )
    return evaluation
