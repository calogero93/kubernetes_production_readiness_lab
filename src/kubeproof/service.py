"""Application use case for a static Helm qualification."""

from __future__ import annotations

import hashlib
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import yaml

from kubeproof import __version__
from kubeproof.analysis import analyze
from kubeproof.bundle import canonical_json_bytes, write_bundle
from kubeproof.domain import (
    AdmissionOutcome,
    EvaluationResult,
    ExecutionOptions,
    InputIdentity,
    Observation,
    ObservationProvenance,
    SourceClass,
)
from kubeproof.fingerprint import capture_tool_fingerprint
from kubeproof.helm import HelmRenderer, HelmRenderRequest, HelmRenderResult
from kubeproof.manifests import parse_manifests, redact_manifest
from kubeproof.profile import CompanyProfile
from kubeproof.runtime import run_runtime
from kubeproof.safety import decide_local_admission


class ManifestRenderer(Protocol):
    def render(self, request: HelmRenderRequest) -> HelmRenderResult: ...


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
) -> EvaluationResult:
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
    resources = parse_manifests(render.manifest)
    analysis = analyze(resources, profile)
    admission = decide_local_admission(analysis.observations)
    runtime = (
        run_runtime(
            request=request,
            resources=resources,
            profile=profile,
            install_timeout_seconds=install_timeout_seconds,
            steady_state_seconds=steady_state_seconds,
            max_recovery_targets=max_recovery_targets,
        )
        if execute_known_chart and admission.outcome is AdmissionOutcome.ADMIT
        else None
    )
    static_checks = tuple(check for check in analysis.checks if not check.id.startswith("runtime."))
    untested = tuple(check for check in analysis.checks if check.id.startswith("runtime."))
    if admission.outcome is AdmissionOutcome.STATIC_ONLY:
        rules = ", ".join(admission.matched_rule_ids)
        untested = tuple(
            check.model_copy(
                update={"explanation": f"Local safety admission stopped execution: {rules}"}
            )
            for check in untested
        )
    normalized_profile = profile.model_dump(mode="json")
    manifest_sha256 = hashlib.sha256(render.manifest).hexdigest()
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
            chart_package_sha256=_sha256_file(Path(chart)) if Path(chart).is_file() else None,
            rendered_manifest_sha256=manifest_sha256,
            values_sha256=tuple(_sha256_file(path) for path in values_files),
            set_values_sha256=tuple(
                hashlib.sha256(value.encode("utf-8")).hexdigest() for value in set_values
            ),
            profile_sha256=hashlib.sha256(canonical_json_bytes(normalized_profile)).hexdigest(),
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
        ),
        admission=admission,
        checks=static_checks + (runtime.checks if runtime else untested),
        observations=observations,
        findings=analysis.findings + (runtime.findings if runtime else ()),
        environment=runtime.environment if runtime else None,
        tool_fingerprint=capture_tool_fingerprint(
            helm_executable=helm._executable if isinstance(helm, HelmRenderer) else None,
            used_kind=runtime is not None,
        ),
    )
    write_bundle(
        output,
        evaluation,
        normalized_profile=normalized_profile,
        redacted_manifest=redact_manifest(resources),
        artifacts=runtime.artifacts if runtime else None,
    )
    return evaluation
