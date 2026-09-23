"""Filesystem evidence bundle writer."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from kubeproof.domain import EvaluationResult, SourceClass, StrictModel
from kubeproof.report import render_markdown


class BundleError(RuntimeError):
    pass


class BundleManifest(StrictModel):
    schema_version: Literal["1"]
    evaluation_id: str
    files: dict[str, str]
    observations: dict[str, str]


@dataclass(frozen=True)
class BundleVerification:
    evaluation: EvaluationResult
    evaluation_sha256: str
    bundle_sha256: str | None


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _observation_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BundleError(f"cannot read bundle file {path}: {exc}") from exc
    return digest.hexdigest()


def _bundle_files(root: Path) -> set[str]:
    if root.is_symlink() or not root.is_dir():
        raise BundleError(f"bundle is not a regular directory: {root}")
    names: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise BundleError(f"bundle contains an unsafe entry: {path.relative_to(root)}")
        if path.is_file():
            names.add(path.relative_to(root).as_posix())
    required = {
        "evaluation.json",
        "report.md",
        "profile.normalized.json",
        "input/rendered-manifests.redacted.yaml",
    }
    if missing := required - names:
        raise BundleError(f"bundle is incomplete; missing: {', '.join(sorted(missing))}")
    return names


def verify_bundle(root: Path) -> BundleVerification:
    """Validate an offline bundle; legacy 0.1 bundles remain readable but unsealed."""
    names = _bundle_files(root)
    try:
        evaluation_bytes = (root / "evaluation.json").read_bytes()
        evaluation = EvaluationResult.model_validate_json(evaluation_bytes)
    except (OSError, ValidationError, ValueError) as exc:
        raise BundleError(f"invalid evaluation bundle {root}: {exc}") from exc
    evaluation_sha256 = hashlib.sha256(evaluation_bytes).hexdigest()
    if evaluation.schema_version == "0.1":
        if "bundle-manifest.json" in names:
            raise BundleError("legacy evaluation cannot claim a sealed bundle")
        return BundleVerification(evaluation, evaluation_sha256, None)

    if "bundle-manifest.json" not in names:
        raise BundleError("schema 0.2 bundle has no integrity manifest")
    try:
        manifest_bytes = (root / "bundle-manifest.json").read_bytes()
        manifest = BundleManifest.model_validate_json(manifest_bytes)
    except (OSError, ValidationError, ValueError) as exc:
        raise BundleError(f"invalid bundle integrity manifest: {exc}") from exc
    if manifest.evaluation_id != evaluation.evaluation_id:
        raise BundleError("bundle integrity manifest refers to a different evaluation")
    if set(manifest.files) != names - {"bundle-manifest.json"}:
        raise BundleError("bundle file list differs from the integrity manifest")
    for name, expected in manifest.files.items():
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or _sha256_file(root / name) != expected:
            raise BundleError(f"bundle file failed SHA-256 validation: {name}")
    observation_hashes = {
        item.id: _observation_sha256(item.model_dump(mode="json"))
        for item in evaluation.observations
    }
    if manifest.observations != observation_hashes:
        raise BundleError("sealed observation digests do not match evaluation.json")
    try:
        report = (root / "report.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BundleError(f"cannot read report.md: {exc}") from exc
    if report != render_markdown(evaluation):
        raise BundleError("report.md is not the deterministic rendering of evaluation.json")
    if _sha256_file(root / "profile.normalized.json") != evaluation.input.profile_sha256:
        raise BundleError("profile snapshot does not match the evaluation input identity")
    for observation in evaluation.observations:
        if observation.source_class is SourceClass.STATIC_INPUT:
            if observation.provenance is None or (
                observation.provenance.source_sha256
                != evaluation.input.rendered_manifest_sha256
            ):
                raise BundleError(f"static observation {observation.id} has invalid provenance")
        elif observation.source_class is SourceClass.RUNTIME and (
            evaluation.environment is None
            or observation.provenance is None
            or observation.provenance.source_ref
            != f"environment:kind:{evaluation.environment.cluster_name}"
        ):
            raise BundleError(f"runtime observation {observation.id} has invalid provenance")
    return BundleVerification(
        evaluation, evaluation_sha256, hashlib.sha256(manifest_bytes).hexdigest()
    )


def write_bundle(
    output: Path,
    evaluation: EvaluationResult,
    *,
    normalized_profile: dict[str, object],
    redacted_manifest: str,
    artifacts: dict[str, str] | None = None,
) -> None:
    if output.is_symlink():
        raise BundleError(f"output path is a symbolic link: {output}")
    if output.exists():
        if not output.is_dir():
            raise BundleError(f"output path is not a directory: {output}")
        if any(output.iterdir()):
            raise BundleError(f"output directory is not empty: {output}")

    temporary_output: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
        (temporary_output / "input").mkdir()
        (temporary_output / "evaluation.json").write_bytes(
            canonical_json_bytes(evaluation.model_dump(mode="json"))
        )
        (temporary_output / "report.md").write_text(render_markdown(evaluation), encoding="utf-8")
        (temporary_output / "profile.normalized.json").write_bytes(
            canonical_json_bytes(normalized_profile)
        )
        (temporary_output / "input" / "rendered-manifests.redacted.yaml").write_text(
            redacted_manifest, encoding="utf-8"
        )
        for name, content in (artifacts or {}).items():
            relative = Path(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or len(relative.parts) < 2
                or relative.parts[0] != "artifacts"
            ):
                raise BundleError(f"invalid artifact path: {name}")
            destination = temporary_output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")

        if evaluation.schema_version == "0.2":
            manifest = BundleManifest(
                schema_version="1",
                evaluation_id=evaluation.evaluation_id,
                files={
                    name: _sha256_file(temporary_output / name)
                    for name in sorted(_bundle_files(temporary_output))
                },
                observations={
                    item.id: _observation_sha256(item.model_dump(mode="json"))
                    for item in evaluation.observations
                },
            )
            (temporary_output / "bundle-manifest.json").write_bytes(
                canonical_json_bytes(manifest.model_dump(mode="json"))
            )
            verify_bundle(temporary_output)

        # A reader can now observe either no bundle or the complete bundle, never a
        # partially written one. Existing empty output directories are retained as a
        # supported CLI input and removed only immediately before the atomic promotion.
        if output.exists():
            output.rmdir()
        temporary_output.replace(output)
        temporary_output = None
    except OSError as exc:
        raise BundleError(f"cannot write bundle {output}: {exc}") from exc
    finally:
        if temporary_output is not None:
            shutil.rmtree(temporary_output, ignore_errors=True)
