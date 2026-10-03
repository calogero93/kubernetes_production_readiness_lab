from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from kubeproof.application.service import inspect_chart
from kubeproof.core.domain import (
    AdmissionOutcome,
    EnvironmentInfo,
    EvaluationResult,
    HttpProbeOptions,
    OperatorApproval,
    SourceClass,
)
from kubeproof.core.manifests import parse_manifests
from kubeproof.core.plans import EvaluationPlan, HttpTask
from kubeproof.core.profile import CompanyProfile
from kubeproof.evidence.bundle import BundleError, canonical_json_bytes, verify_bundle, write_bundle
from kubeproof.evidence.history import HistoryError, HistoryService
from kubeproof.execution.helm import HelmRenderRequest, HelmRenderResult
from kubeproof.execution.runtime import RuntimeResult


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
    assert all(check.experiment_version == "1" for check in evaluation.checks)
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
    monkeypatch.setattr("kubeproof.application.service._chart_archive_version", lambda _: "1")
    monkeypatch.setattr(
        "kubeproof.application.service.run_runtime",
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


HOST_MANIFEST = b"""\
apiVersion: v1
kind: Pod
metadata: {name: host-reader}
spec:
  hostNetwork: true
  containers:
    - name: reader
      image: example/reader:1
"""


def test_host_access_requires_exact_approval_and_records_it(
    tmp_path: Path, strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "host-reader.tgz"
    archive.write_bytes(b"pinned chart")
    monkeypatch.setattr("kubeproof.application.service._chart_archive_version", lambda _: "1")
    runtime_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "kubeproof.application.service.run_runtime",
        lambda **kwargs: runtime_calls.append(kwargs["approved_host_rule_ids"]),
    )
    inputs = dict(
        chart=str(archive),
        version=None,
        profile=strict_profile,
        values_files=(),
        set_values=(),
        renderer=FakeRenderer(HOST_MANIFEST),
    )
    preflight = inspect_chart(**inputs, output=tmp_path / "preflight")
    assert preflight.admission.outcome is AdmissionOutcome.STATIC_ONLY
    assert preflight.admission.matched_rule_ids == ("KP-SAFE-002",)
    scope = preflight.admission.approval_scope_sha256
    assert scope is not None
    assert runtime_calls == []

    approval = OperatorApproval(
        operator_label="lab-operator",
        reason="Authorized isolated workstation test",
        scope_sha256=scope,
        approved_at=datetime.now(UTC),
    )
    approved = inspect_chart(
        **inputs,
        output=tmp_path / "approved",
        execute_known_chart=True,
        operator_approval=approval,
    )
    assert approved.admission.outcome is AdmissionOutcome.ADMIT_WITH_OPERATOR_APPROVAL
    assert approved.admission.operator_approval == approval
    assert runtime_calls == [("KP-SAFE-002",)]
    assert verify_bundle(tmp_path / "approved").bundle_sha256
    report = (tmp_path / "approved" / "report.md").read_text()
    assert "lab-operator" in report
    assert "identity was not authenticated" in report

    with pytest.raises(ValueError, match="does not match"):
        inspect_chart(
            **{**inputs, "renderer": FakeRenderer(HOST_MANIFEST + b"\n# changed\n")},
            output=tmp_path / "changed-manifest",
            execute_known_chart=True,
            operator_approval=approval,
        )
    with pytest.raises(ValueError, match="does not match"):
        inspect_chart(
            **{**inputs, "profile": strict_profile.model_copy(update={"name": "other-profile"})},
            output=tmp_path / "changed-profile",
            execute_known_chart=True,
            operator_approval=approval,
        )
    values = tmp_path / "values.yaml"
    values.write_text("replicas: 2\n")
    with pytest.raises(ValueError, match="does not match"):
        inspect_chart(
            **{**inputs, "values_files": (values,)},
            output=tmp_path / "changed-values",
            execute_known_chart=True,
            operator_approval=approval,
        )
    archive.write_bytes(b"different chart")
    with pytest.raises(ValueError, match="does not match"):
        inspect_chart(
            **inputs,
            output=tmp_path / "changed-chart",
            execute_known_chart=True,
            operator_approval=approval,
        )
    assert runtime_calls == [("KP-SAFE-002",)]


def test_non_host_hard_stop_cannot_be_approved(
    tmp_path: Path,
    strict_profile: CompanyProfile,
    unsafe_manifest: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "unsafe.tgz"
    archive.write_bytes(b"pinned chart")
    monkeypatch.setattr("kubeproof.application.service._chart_archive_version", lambda _: "1")
    preflight = inspect_chart(
        chart=str(archive),
        version=None,
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=tmp_path / "preflight",
        renderer=FakeRenderer(unsafe_manifest),
    )
    assert preflight.admission.approval_scope_sha256 is None
    approval = OperatorApproval(
        operator_label="operator",
        reason="test",
        scope_sha256="0" * 64,
        approved_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="only to an executable host-access chart"):
        inspect_chart(
            chart=str(archive),
            version=None,
            profile=strict_profile,
            values_files=(),
            set_values=(),
            output=tmp_path / "disallowed",
            renderer=FakeRenderer(unsafe_manifest),
            execute_known_chart=True,
            operator_approval=approval,
        )


def test_http_preflight_records_options_as_not_tested_without_execution(
    tmp_path: Path,
    strict_profile: CompanyProfile,
    http_manifest: bytes,
) -> None:
    options = HttpProbeOptions(service="api", port=8080, max_p95_ms=100)
    result = inspect_chart(
        chart="./chart",
        version=None,
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=tmp_path / "preflight",
        renderer=FakeRenderer(http_manifest),
        http_probe=options,
    )
    check = next(check for check in result.checks if check.id == "runtime.http_service")
    assert check.assessment == "not_tested"
    assert not any(obs.observation_type == "http.service_probe" for obs in result.observations)
    assert result.execution_options.http_probe == options
    assert verify_bundle(tmp_path / "preflight").bundle_sha256


def test_changing_http_parameters_invalidates_host_access_approval(
    tmp_path: Path,
    strict_profile: CompanyProfile,
    http_manifest: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "http-host.tgz"
    archive.write_bytes(b"chart")
    monkeypatch.setattr("kubeproof.application.service._chart_archive_version", lambda _: "1")
    renderer = FakeRenderer(http_manifest + b"\n---\n" + HOST_MANIFEST)
    inputs = dict(
        chart=str(archive),
        version=None,
        profile=strict_profile,
        values_files=(),
        set_values=(),
        renderer=renderer,
    )
    options = HttpProbeOptions(service="api", port=8080)
    preflight = inspect_chart(**inputs, http_probe=options, output=tmp_path / "preview")
    approval = OperatorApproval(
        operator_label="operator",
        reason="local test",
        scope_sha256=preflight.admission.approval_scope_sha256,
        approved_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="does not match"):
        inspect_chart(
            **inputs,
            http_probe=options.model_copy(update={"path": "/other"}),
            output=tmp_path / "changed",
            execute_known_chart=True,
            operator_approval=approval,
        )


@pytest.mark.parametrize("status", [200, 503])
def test_http_runtime_evidence_is_sealed_and_indexed(
    tmp_path: Path,
    strict_profile: CompanyProfile,
    http_manifest: bytes,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    archive = tmp_path / "http.tgz"
    archive.write_bytes(b"test chart")
    monkeypatch.setattr("kubeproof.application.service._chart_archive_version", lambda _: "1")
    measurement = {
        "schema_version": "2",
        "target": "http://api.kubeproof-product.svc:8080/healthz",
        "requests": 10,
        "outcomes": {str(status): 10},
        "p95_ms": 10.0,
        "latencies_ms": [10.0] * 10,
        "all_responses_200": status == 200,
    }
    monkeypatch.setattr(
        "kubeproof.execution.http_service.run_service_probe", lambda **_: measurement
    )

    def runtime(**kwargs: Any) -> RuntimeResult:
        service = parse_manifests(http_manifest)[0]
        cluster = SimpleNamespace(
            core=SimpleNamespace(read_namespaced_service=lambda *_args, **_kwargs: service),
            api_client=SimpleNamespace(sanitize_for_serialization=lambda item: item),
        )
        evidence = kwargs["experiment"].run(
            cluster, Path("config"), "kubeproof-product", lambda: False
        )
        return RuntimeResult(
            checks=evidence.checks,
            observations=evidence.observations,
            findings=evidence.findings,
            artifacts=evidence.artifacts,
            plan_execution=kwargs["experiment"].execution,
            environment=EnvironmentInfo(
                provider="kind",
                cluster_name="test-http",
                cleanup_attempted=True,
                cleanup_succeeded=True,
            ),
        )

    monkeypatch.setattr("kubeproof.application.service.run_runtime", runtime)
    output = tmp_path / "bundle"
    result = inspect_chart(
        chart=str(archive),
        version=None,
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=output,
        renderer=FakeRenderer(http_manifest),
        execute_known_chart=True,
        http_probe=HttpProbeOptions(service="api", port=8080, max_p95_ms=100),
        test_plan=EvaluationPlan(
            objective="Check HTTP behavior.",
            tasks=(
                HttpTask(
                    id="http",
                    rationale="HTTP contract",
                    parameters=HttpProbeOptions(service="api", port=8080, max_p95_ms=100),
                ),
            ),
        ),
        steady_state_seconds=0,
    )
    check = next(check for check in result.checks if check.id == "runtime.http_service")
    assert check.assessment == ("pass" if status == 200 else "fail")
    http_obs = next(
        obs for obs in result.observations if obs.observation_type == "http.service_probe"
    )
    assert http_obs.provenance.source_ref == "environment:kind:test-http"
    assert (output / "artifacts/probes/http/http/service-probe.json").is_file()
    assert "Observed p95 milliseconds" in (output / "report.md").read_text()
    assert verify_bundle(output).bundle_sha256
    history = HistoryService.local(tmp_path / "history")
    history.import_bundle(output)
    assert history.get_evaluation(result.evaluation_id) == result


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


def test_report_must_match_evaluation_even_with_updated_file_hash(
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
    report_path = output / "report.md"
    report = report_path.read_text().replace("# KubeProof evaluation", "# Changed conclusion")
    report_path.write_text(report)
    manifest_path = output / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["report.md"] = hashlib.sha256(report.encode()).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(BundleError, match="deterministic rendering"):
        verify_bundle(output)


def test_schema_02_rejects_observation_with_wrong_source_class(
    tmp_path: Path, strict_profile: CompanyProfile
) -> None:
    evaluation = inspect_chart(
        chart="./chart",
        version="1.0.0",
        profile=strict_profile,
        values_files=(),
        set_values=(),
        output=tmp_path / "bundle",
        renderer=FakeRenderer(b"apiVersion: v1\nkind: ConfigMap\nmetadata: {name: example}\n"),
    )
    payload = evaluation.model_dump(mode="json")
    payload["observations"][0]["source_class"] = "runtime"

    with pytest.raises(ValidationError, match="mismatched source class"):
        EvaluationResult.model_validate(payload)


def test_bundle_rejects_unlisted_artifact(tmp_path: Path, strict_profile: CompanyProfile) -> None:
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
    artifact.parent.mkdir(exist_ok=True)
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


def test_same_evaluation_id_cannot_replace_sealed_artifacts(
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
    alternate = tmp_path / "alternate"
    write_bundle(
        alternate,
        evaluation,
        normalized_profile=strict_profile.model_dump(mode="json"),
        redacted_manifest="---\n",
        artifacts={"artifacts/kubernetes/pods.json": "[]\n"},
    )
    service = HistoryService.local(tmp_path / "history")
    service.import_bundle(initial)

    with pytest.raises(HistoryError, match="different content"):
        service.import_bundle(alternate)
