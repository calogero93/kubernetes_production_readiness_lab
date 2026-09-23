"""KubeProof command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from kubeproof.bundle import BundleError, verify_bundle
from kubeproof.domain import Severity
from kubeproof.helm import HelmRenderError
from kubeproof.history import HistoryError, HistoryService
from kubeproof.manifests import ManifestError
from kubeproof.profile import CompanyProfile, load_profile
from kubeproof.reproducibility import compare_evaluations
from kubeproof.service import inspect_chart
from kubeproof.web import serve_history

app = typer.Typer(
    name="kubeproof",
    no_args_is_help=True,
    help="Qualify Kubernetes products using attributable evidence.",
)
history_app = typer.Typer(help="Import, rebuild and inspect the local evaluation history.")
app.add_typer(history_app, name="history")


@app.callback()
def main() -> None:
    """KubeProof command group."""


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
    history_dir: Annotated[
        Path | None,
        typer.Option("--history-dir", help="Also import the completed bundle into local history."),
    ] = None,
) -> None:
    """Inspect a Helm product, optionally executing a known product in kind."""
    try:
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
        )
    except (ValueError, ValidationError, HelmRenderError, ManifestError, BundleError) as exc:
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
        serve_history(service, host, port)
    except OSError as exc:
        typer.echo(f"error: cannot start history server: {exc}", err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
