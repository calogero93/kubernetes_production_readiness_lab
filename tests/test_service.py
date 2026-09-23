from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kubeproof.bundle import BundleError, canonical_json_bytes, verify_bundle, write_bundle
from kubeproof.domain import SourceClass
from kubeproof.helm import HelmRenderRequest, HelmRenderResult
from kubeproof.history import HistoryError, HistoryService
from kubeproof.profile import CompanyProfile
from kubeproof.service import inspect_chart


class FakeRenderer:
    def __init__(self, manifest: bytes) -> None:
        self.manifest = manifest
        self.request: HelmRenderRequest | None = None

    def render(self, request: HelmRenderRequest) -> HelmRenderResult:
        self.request = request
        return HelmRenderResult(manifest=self.manifest, stderr="")


def test_inspection_writes_canonical_bundle_and_redacts_secrets(
    tmp_path: Path, strict_profile: CompanyProfile
) -> None:
    manifest = b"""\
apiVersion: v1
kind: Secret
metadata: {name: credential, namespace: product}
stringData:
  password: super-secret
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: safe, namespace: product}
spec:
  replicas: 2
  selector: {matchLabels: {app: safe}}
  template:
    metadata: {labels: {app: safe}}
    spec:
      securityContext: {runAsNonRoot: true}
      containers:
        - name: safe
          image: example/safe:1
          resources:
            requests: {cpu: 100m, memory: 64Mi}
            limits: {cpu: 200m, memory: 128Mi}
          securityContext: {readOnlyRootFilesystem: true}
"""
    output = tmp_path / "bundle"
    renderer = FakeRenderer(manifest)

    evaluation = inspect_chart(
        chart="./chart",
        version="1.0.0",
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=output,
        renderer=renderer,
    )

    persisted = json.loads((output / "evaluation.json").read_text())
    artifact = (output / "input" / "rendered-manifests.redacted.yaml").read_text()
    assert (
        persisted["input"]["rendered_manifest_sha256"] == evaluation.input.rendered_manifest_sha256
    )
    assert "super-secret" not in artifact
    assert "<redacted:sha256:" in artifact
    assert (output / "report.md").exists()
    assert (output / "bundle-manifest.json").exists()
    assert verify_bundle(output).bundle_sha256 is not None
    assert evaluation.schema_version == "0.2"
    assert all(observation.provenance is not None for observation in evaluation.observations)
    assert all(
        observation.provenance.source_sha256 == evaluation.input.rendered_manifest_sha256
        for observation in evaluation.observations
        if observation.source_class is SourceClass.STATIC_INPUT
        and observation.provenance is not None
    )
    assert not tuple(tmp_path.glob(".bundle.tmp-*"))
    assert all(
        check["assessment"] != "pass"
        for check in persisted["checks"]
        if check["id"].startswith("runtime.")
    )


def test_identical_static_inputs_produce_identical_facts(
    tmp_path: Path, strict_profile: CompanyProfile
) -> None:
    manifest = b"""\
apiVersion: apps/v1
kind: Deployment
metadata: {name: example}
spec:
  replicas: 2
  template:
    spec:
      containers:
      - name: app
        image: example:v1
"""
    runs = [
        inspect_chart(
            chart="./chart",
            version="1.0.0",
            profile=strict_profile,
            values_files=(),
            set_values=(),
            output=tmp_path / f"run-{number}",
            renderer=FakeRenderer(manifest),
        )
        for number in (1, 2)
    ]

    assert runs[0].checks == runs[1].checks
    assert runs[0].observations == runs[1].observations
    assert runs[0].findings == runs[1].findings


def test_static_only_explains_why_runtime_checks_did_not_run(
    tmp_path: Path,
    strict_profile: CompanyProfile,
    unsafe_manifest: bytes,
) -> None:
    result = inspect_chart(
        chart="./unsafe-chart",
        version="1",
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=tmp_path / "static-only",
        renderer=FakeRenderer(unsafe_manifest),
        execute_known_chart=False,
    )

    assert result.admission.matched_rule_ids
    assert all(
        "KP-SAFE-" in (check.explanation or "")
        for check in result.checks
        if check.id.startswith("runtime.")
    )


def test_unsafe_chart_never_starts_kind_even_when_runtime_requested(
    tmp_path: Path,
    strict_profile: CompanyProfile,
    unsafe_manifest: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "unsafe.tgz"
    archive.write_bytes(b"chart package placeholder")
    monkeypatch.setattr("kubeproof.service._chart_archive_version", lambda _: "1")
    monkeypatch.setattr(
        "kubeproof.service.run_runtime",
        lambda **_: (_ for _ in ()).throw(AssertionError("runtime must not start")),
    )

    result = inspect_chart(
        chart=str(archive),
        version="1",
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=tmp_path / "unsafe-bundle",
        renderer=FakeRenderer(unsafe_manifest),
        execute_known_chart=True,
    )

    assert result.admission.outcome == "static_only"
    assert result.environment is None


@pytest.mark.parametrize(
    "relative_path",
    [
        "evaluation.json",
        "report.md",
        "profile.normalized.json",
        "input/rendered-manifests.redacted.yaml",
        "bundle-manifest.json",
    ],
)
def test_sealed_bundle_detects_file_tampering(
    tmp_path: Path, strict_profile: CompanyProfile, relative_path: str
) -> None:
    output = tmp_path / "bundle"
    inspect_chart(
        chart="./chart",
        version="1.0.0",
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=output,
        renderer=FakeRenderer(b"apiVersion: v1\nkind: ConfigMap\nmetadata: {name: example}\n"),
    )
    target = output / relative_path
    target.write_bytes(target.read_bytes() + b"\nchanged\n")

    with pytest.raises(BundleError):
        verify_bundle(output)


def test_observation_seal_detects_modified_evidence_with_updated_file_hash(
    tmp_path: Path, strict_profile: CompanyProfile
) -> None:
    output = tmp_path / "bundle"
    inspect_chart(
        chart="./chart",
        version="1.0.0",
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=output,
        renderer=FakeRenderer(b"apiVersion: v1\nkind: ConfigMap\nmetadata: {name: example}\n"),
    )
    evaluation_path = output / "evaluation.json"
    evaluation = json.loads(evaluation_path.read_text())
    assert evaluation["observations"]
    evaluation["observations"][0]["summary"] = "Altered observation"
    payload = canonical_json_bytes(evaluation)
    evaluation_path.write_bytes(payload)
    manifest_path = output / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["evaluation.json"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(BundleError, match="sealed observation digests"):
        verify_bundle(output)


def test_bundle_rejects_unlisted_artifact(
    tmp_path: Path, strict_profile: CompanyProfile
) -> None:
    output = tmp_path / "bundle"
    inspect_chart(
        chart="./chart",
        version="1.0.0",
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=output,
        renderer=FakeRenderer(b"apiVersion: v1\nkind: ConfigMap\nmetadata: {name: example}\n"),
    )
    artifact = output / "artifacts" / "unexpected.txt"
    artifact.parent.mkdir()
    artifact.write_text("not in seal")

    with pytest.raises(BundleError, match="file list differs"):
        verify_bundle(output)


def test_artifact_hash_is_checked_on_history_import(
    tmp_path: Path, strict_profile: CompanyProfile
) -> None:
    initial = tmp_path / "initial"
    evaluation = inspect_chart(
        chart="./chart",
        version="1.0.0",
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=initial,
        renderer=FakeRenderer(b"apiVersion: v1\nkind: ConfigMap\nmetadata: {name: example}\n"),
    )
    with_artifact = tmp_path / "with-artifact"
    write_bundle(
        with_artifact,
        evaluation,
        normalized_profile=strict_profile.model_dump(mode="json"),
        redacted_manifest="---\n",
        artifacts={"artifacts/kubernetes/pods.json": "[]\n"},
    )
    artifact = with_artifact / "artifacts" / "kubernetes" / "pods.json"
    artifact.write_text("[{}]\n")

    with pytest.raises(BundleError, match="failed SHA-256"):
        verify_bundle(with_artifact)
    with pytest.raises(HistoryError, match="failed SHA-256"):
        HistoryService.local(tmp_path / "history").import_bundle(with_artifact)
