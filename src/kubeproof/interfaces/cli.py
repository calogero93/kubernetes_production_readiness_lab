"""KubeProof command-line interface."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from kubeproof.application.service import inspect_chart
from kubeproof.core.domain import HttpProbeOptions, OperatorApproval, Severity
from kubeproof.core.manifests import ManifestError
from kubeproof.core.plans import EvaluationPlan, PlanBudget
from kubeproof.core.profile import CompanyProfile, load_profile
from kubeproof.evidence.bundle import BundleError, verify_bundle
from kubeproof.evidence.history import HistoryError, HistoryService
from kubeproof.evidence.reproducibility import compare_evaluations
from kubeproof.execution.helm import HelmRenderError
from kubeproof.interfaces.web import serve_history

app = typer.Typer(
    name="kubeproof",
    no_args_is_help=True,
    help="Qualify Kubernetes products using attributable evidence.",
)
history_app = typer.Typer(help="Import, rebuild and inspect the local evaluation history.")
ai_app = typer.Typer(
    help="Preview AI requirement extraction and CPU test plans; never execute tests."
)
app.add_typer(history_app, name="history")
app.add_typer(ai_app, name="ai")


@app.callback()
def main() -> None:
    """KubeProof command group."""


@ai_app.command("schema")
def ai_schema() -> None:
    """Print the confirmed CPU evaluation request schema."""
    from kubeproof.intelligence.models import ConfirmedRequest

    typer.echo(json.dumps(ConfirmedRequest.model_json_schema(), indent=2))


@ai_app.command("doctor")
def ai_doctor() -> None:
    """Check a configured llama.cpp or OpenAI-compatible server without inference."""
    try:
        from kubeproof.intelligence.model_config import (
            ModelConfigurationError,
            ModelProbeError,
            probe_compatible_model,
            probe_llama_cpp,
        )

        provider = os.environ.get("KUBEPROOF_AI_PROVIDER", "").strip()
        status = probe_llama_cpp() if provider == "llama_cpp" else probe_compatible_model()
    except ImportError as exc:
        typer.echo("error: install the AI extra with 'uv sync --extra ai'", err=True)
        raise typer.Exit(code=1) from exc
    except (ModelConfigurationError, ModelProbeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Model available: {status.model_id} at {status.base_url}")


@ai_app.command("draft")
def ai_draft(
    description: Annotated[str, typer.Argument(help="Natural-language evaluation request.")],
) -> None:
    """Extract a reviewable draft; it is never an executable policy."""
    try:
        from kubeproof.intelligence.model_adapter import (
            InvalidRequirementDraft,
            LangChainRequirementsExtractor,
        )
        from kubeproof.intelligence.model_config import ModelConfigurationError, create_chat_model

        result = LangChainRequirementsExtractor(create_chat_model()).extract(description)
    except ImportError as exc:
        typer.echo("error: install the AI extra with 'uv sync --extra ai'", err=True)
        raise typer.Exit(code=1) from exc
    except (InvalidRequirementDraft, ModelConfigurationError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(result.model_dump_json(indent=2))


@ai_app.command("plan")
def ai_plan(
    request_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="Confirmed request JSON."),
    ],
) -> None:
    """Generate and validate a CPU test plan without running any test."""
    try:
        from kubeproof.intelligence.capabilities import cpu_capabilities
        from kubeproof.intelligence.control import PlanRejected, validate_plan
        from kubeproof.intelligence.model_adapter import (
            InvalidModelPlan,
            LangChainSupervisor,
        )
        from kubeproof.intelligence.model_config import ModelConfigurationError, create_chat_model
        from kubeproof.intelligence.models import ConfirmedRequest

        request = ConfirmedRequest.model_validate_json(request_path.read_bytes())
        catalog = cpu_capabilities()
        plan = LangChainSupervisor(create_chat_model()).propose(request, None, (), catalog)
        validate_plan(request, plan, catalog=catalog)
    except ImportError as exc:
        typer.echo("error: install the AI extra with 'uv sync --extra ai'", err=True)
        raise typer.Exit(code=1) from exc
    except (
        OSError,
        ValidationError,
        InvalidModelPlan,
        ModelConfigurationError,
        PlanRejected,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(plan.model_dump_json(indent=2))


@app.command("catalog")
def probe_catalog() -> None:
    """Describe registered probes and the common immutable plan schema."""
    from kubeproof.execution.probes import catalog_context

    typer.echo(json.dumps(catalog_context(), indent=2))


@app.command("probe-service")
def probe_service(
    service: Annotated[str, typer.Argument(help="Service name in the target namespace.")],
    namespace: Annotated[str, typer.Option("--namespace")],
    image: Annotated[str, typer.Option("--image", help="Pullable KubeProof image for the Job.")],
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 80,
    path: Annotated[str, typer.Option("--path")] = "/healthz",
    requests: Annotated[int, typer.Option("--requests", min=1, max=50)] = 10,
    context: Annotated[str | None, typer.Option("--context")] = None,
) -> None:
    """Send bounded HTTP traffic through a Service from inside Kubernetes."""
    from kubeproof.execution.service_probe import ServiceProbeError, run_service_probe

    try:
        result = run_service_probe(
            service=service,
            namespace=namespace,
            port=port,
            path=path,
            requests=requests,
            image=image,
            context=context,
        )
    except (ValueError, ServiceProbeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result, indent=2, sort_keys=True))
    if result["outcomes"] != {"200": requests}:
        raise typer.Exit(code=2)


@app.command()
def inspect(
    chart: Annotated[str, typer.Argument(help="Helm chart reference, OCI URL, or local path")],
    profile_path: Annotated[
        Path, typer.Option("--profile", exists=True, dir_okay=False, readable=True)
    ],
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("kubeproof-run"),
    version: Annotated[str | None, typer.Option("--version")] = None,
    values: Annotated[
        list[Path] | None,
        typer.Option("--values", exists=True, dir_okay=False, readable=True),
    ] = None,
    set_value: Annotated[list[str] | None, typer.Option("--set")] = None,
    kubernetes_version: Annotated[str | None, typer.Option("--kubernetes-version")] = None,
    render_timeout: Annotated[int, typer.Option("--render-timeout", min=1, max=600)] = 90,
    execute_known_chart: Annotated[
        bool,
        typer.Option(
            "--execute-known-chart",
            help="Run this intentionally selected chart in disposable kind.",
        ),
    ] = False,
    install_timeout: Annotated[int, typer.Option("--install-timeout", min=30, max=1800)] = 300,
    steady_state_window: Annotated[int, typer.Option("--steady-state-window", min=0, max=600)] = 60,
    max_recovery_targets: Annotated[int, typer.Option("--max-recovery-targets", min=0, max=5)] = 5,
    approve_local_risk: Annotated[
        str | None,
        typer.Option(
            "--approve-local-risk", help="Approval scope SHA-256 from a static preflight."
        ),
    ] = None,
    operator_label: Annotated[str | None, typer.Option("--operator-label")] = None,
    approval_reason: Annotated[str | None, typer.Option("--approval-reason")] = None,
    http_service: Annotated[
        str | None, typer.Option("--http-service", help="Rendered ClusterIP Service to probe.")
    ] = None,
    http_port: Annotated[int, typer.Option("--http-port", min=1, max=65535)] = 80,
    http_path: Annotated[str, typer.Option("--http-path")] = "/healthz",
    http_requests: Annotated[int, typer.Option("--http-requests", min=1, max=50)] = 10,
    http_expected_status: Annotated[
        int, typer.Option("--http-expected-status", min=100, max=599)
    ] = 200,
    http_max_failed_requests: Annotated[
        int, typer.Option("--http-max-failed-requests", min=0, max=49)
    ] = 0,
    http_max_p95_ms: Annotated[float | None, typer.Option("--http-max-p95-ms", min=0.001)] = None,
    plan_path: Annotated[
        Path | None,
        typer.Option(
            "--plan",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Reviewed immutable probe plan JSON.",
        ),
    ] = None,
    ai_plan: Annotated[
        bool, typer.Option("--ai-plan", help="Propose a plan during static preflight only.")
    ] = False,
    objective: Annotated[str | None, typer.Option("--objective")] = None,
    plan_max_tasks: Annotated[int, typer.Option("--plan-max-tasks", min=1, max=32)] = 16,
    plan_max_seconds: Annotated[int, typer.Option("--plan-max-seconds", min=1, max=1800)] = 900,
    plan_parallelism: Annotated[int, typer.Option("--plan-parallelism", min=1, max=4)] = 1,
    ai_interpret: Annotated[
        bool, typer.Option("--ai-interpret", help="Add grounded AI commentary after execution.")
    ] = False,
    history_dir: Annotated[
        Path | None,
        typer.Option("--history-dir", help="Also import the completed bundle into local history."),
    ] = None,
) -> None:
    """Inspect a Helm product, optionally executing a known product in kind."""
    try:
        if plan_path is not None and http_service is not None:
            raise ValueError("use --plan or HTTP shorthand, not both")
        test_plan = (
            EvaluationPlan.model_validate_json(plan_path.read_bytes()) if plan_path else None
        )
        intelligence = None
        if ai_plan or ai_interpret:
            from kubeproof.intelligence.evaluation import EvaluationAI

            intelligence = EvaluationAI()
        if http_service is None and (
            (http_port, http_path, http_requests, http_expected_status, http_max_failed_requests)
            != (80, "/healthz", 10, 200, 0)
            or http_max_p95_ms is not None
        ):
            raise ValueError("HTTP probe options require --http-service")
        http_probe = (
            HttpProbeOptions(
                service=http_service,
                port=http_port,
                path=http_path,
                requests=http_requests,
                expected_status=http_expected_status,
                max_failed_requests=http_max_failed_requests,
                max_p95_ms=http_max_p95_ms,
            )
            if http_service is not None
            else None
        )
        if (operator_label or approval_reason) and not approve_local_risk:
            raise ValueError("operator label and reason require --approve-local-risk")
        if approve_local_risk and (not operator_label or not approval_reason):
            raise ValueError("local-risk approval requires --operator-label and --approval-reason")
        approval = (
            OperatorApproval(
                operator_label=operator_label or "",
                reason=approval_reason or "",
                scope_sha256=approve_local_risk or "",
                approved_at=datetime.now(UTC),
            )
            if approve_local_risk
            else None
        )
        profile = load_profile(profile_path)
        evaluation = inspect_chart(
            chart=chart,
            version=version,
            profile=profile,
            values_files=tuple(values or ()),
            set_values=tuple(set_value or ()),
            output=output,
            kubernetes_version=kubernetes_version,
            render_timeout_seconds=render_timeout,
            execute_known_chart=execute_known_chart,
            install_timeout_seconds=install_timeout,
            steady_state_seconds=steady_state_window,
            max_recovery_targets=max_recovery_targets,
            operator_approval=approval,
            http_probe=http_probe,
            test_plan=test_plan,
            expected_plan_sha256=test_plan.digest() if test_plan else None,
            plan_generator=intelligence.propose if ai_plan and intelligence else None,
            planning_objective=objective,
            plan_budget=PlanBudget(
                max_tasks=plan_max_tasks,
                max_elapsed_seconds=plan_max_seconds,
                max_parallel_tasks=plan_parallelism,
            ),
            interpreter=intelligence.interpret if ai_interpret and intelligence else None,
        )
    except (
        ImportError,
        OSError,
        ValueError,
        ValidationError,
        HelmRenderError,
        ManifestError,
        BundleError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    blockers = sum(item.severity is Severity.BLOCKER for item in evaluation.findings)
    warnings = sum(item.severity is Severity.WARNING for item in evaluation.findings)
    if history_dir is not None:
        try:
            HistoryService.local(history_dir).import_bundle(output)
        except HistoryError as exc:
            typer.echo(f"warning: bundle is valid but history indexing failed: {exc}", err=True)
        else:
            typer.echo(f"History: {history_dir.resolve()}")
    typer.echo(f"Evaluation bundle: {output.resolve()}")
    typer.echo(f"Admission: {evaluation.admission.outcome}")
    if evaluation.execution_options.test_plan:
        typer.echo(f"Frozen plan: {evaluation.execution_options.test_plan.digest()}")
        typer.echo(f"Plan file: {output.resolve() / 'artifacts/plan/plan.json'}")
    if evaluation.admission.approval_scope_sha256:
        typer.echo(
            f"Local host-access approval scope: {evaluation.admission.approval_scope_sha256}"
        )
    typer.echo(f"Findings: {blockers} blocker(s), {warnings} warning(s)")
    if blockers:
        raise typer.Exit(code=2)


@history_app.command("import")
def import_history(
    bundle: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, help="Evaluation bundle."),
    ],
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="Local history data directory."),
    ] = Path(".kubeproof"),
) -> None:
    """Copy a validated bundle into the immutable local store and index it."""
    try:
        evaluation = HistoryService.local(data_dir).import_bundle(bundle)
    except HistoryError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Imported evaluation: {evaluation.evaluation_id}")


@app.command()
def verify(
    bundle: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, help="Evaluation bundle."),
    ],
) -> None:
    """Verify a bundle and its evidence without contacting a cluster."""
    try:
        result = verify_bundle(bundle)
    except BundleError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    status = "sealed" if result.bundle_sha256 is not None else "legacy, unsealed"
    typer.echo(f"Verified evaluation: {result.evaluation.evaluation_id} ({status})")


@app.command()
def compare(
    first: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    second: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
) -> None:
    """Compare two sealed evaluations under the documented runtime tolerances."""
    try:
        first_bundle = verify_bundle(first)
        second_bundle = verify_bundle(second)
        if first_bundle.bundle_sha256 is None or second_bundle.bundle_sha256 is None:
            raise ValueError("repeatability comparison requires sealed bundles")
        profile = CompanyProfile.model_validate_json(
            (first / "profile.normalized.json").read_bytes()
        )
        result = compare_evaluations(first_bundle.evaluation, second_bundle.evaluation, profile)
    except (BundleError, OSError, ValidationError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Repeatability: {result.status}")
    for difference in result.differences:
        typer.echo(f"difference: {difference}")
    for limitation in result.limitations:
        typer.echo(f"limitation: {limitation}")
    if result.status == "different":
        raise typer.Exit(code=2)
    if result.status == "inconclusive":
        raise typer.Exit(code=1)


@history_app.command("sync")
def sync_history(
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="Local history data directory."),
    ] = Path(".kubeproof"),
) -> None:
    """Rebuild the SQLite projection from the stored bundles."""
    try:
        count = HistoryService.local(data_dir).sync()
    except HistoryError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Indexed bundles: {count}")


@history_app.command("list")
def list_history(
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="Local history data directory."),
    ] = Path(".kubeproof"),
) -> None:
    """List locally indexed evaluations."""
    service = HistoryService.local(data_dir)
    for item in service.list_evaluations():
        typer.echo(
            f"{item.generated_at}  {item.evaluation_id}  {item.profile_name}  "
            f"{item.blocker_count} blocker(s)  {item.warning_count} warning(s)  "
            f"{item.bundle_status}"
        )


@app.command()
def serve(
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="Local history data directory."),
    ] = Path(".kubeproof"),
    host: Annotated[str, typer.Option(help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    frontend_dir: Annotated[
        Path | None,
        typer.Option(
            "--frontend-dir",
            help="Built React frontend directory (default: checkout frontend/dist).",
        ),
    ] = None,
) -> None:
    """Serve the local single-user evaluation history UI."""
    try:
        service = HistoryService.local(data_dir)
        count = service.sync()
    except HistoryError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Indexed bundles: {count}")
    typer.echo(f"KubeProof history: http://{host}:{port}")
    try:
        serve_history(service, host, port, frontend_dir)
    except OSError as exc:
        typer.echo(f"error: cannot start history server: {exc}", err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
