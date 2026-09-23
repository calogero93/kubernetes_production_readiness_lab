from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kubeproof.core.domain import (
    AdmissionDecision,
    AdmissionOutcome,
    Assessment,
    CheckResult,
    EvaluationResult,
    ExecutionOptions,
    ExecutionStatus,
    Finding,
    InputIdentity,
    Observation,
    Severity,
    SourceClass,
)
from kubeproof.evidence.history import (
    FilesystemBundleStore,
    HistoryError,
    HistoryService,
    SQLiteEvaluationIndex,
)


def _evaluation(description: str = "one replica") -> EvaluationResult:
    return EvaluationResult(
        evaluation_id="11111111-1111-1111-1111-111111111111",
        generated_at=datetime(2026, 9, 22, 10, 30, tzinfo=UTC),
        tool_version="0.1.0a0",
        profile_name="enterprise-strict",
        input=InputIdentity(
            chart="example/chart",
            requested_version="1.0.0",
            rendered_manifest_sha256="a" * 64,
        ),
        execution_options=ExecutionOptions(
            release_name="target",
            namespace="product",
            render_timeout_seconds=90,
        ),
        admission=AdmissionDecision(
            outcome=AdmissionOutcome.ADMIT,
            explanation="safe for static inspection",
        ),
        checks=(
            CheckResult(
                id="static.availability",
                title="Declared availability",
                execution_status=ExecutionStatus.COMPLETED,
                assessment=Assessment.FAIL,
                observation_ids=("obs-1",),
                finding_ids=("finding-1",),
            ),
        ),
        observations=(
            Observation(
                id="obs-1",
                check_id="static.availability",
                source_class=SourceClass.STATIC_INPUT,
                observation_type="availability.deployment_replicas",
                summary="Deployment replica count was inspected.",
                data={"replicas": 1},
            ),
        ),
        findings=(
            Finding(
                id="finding-1",
                check_id="static.availability",
                severity=Severity.BLOCKER,
                title="Replica count below minimum",
                description=description,
                observation_ids=("obs-1",),
            ),
        ),
    )


def _write_bundle(path: Path, evaluation: EvaluationResult) -> None:
    (path / "input").mkdir(parents=True)
    (path / "evaluation.json").write_text(evaluation.model_dump_json(indent=2) + "\n")
    (path / "report.md").write_text("# Evaluation\n")
    (path / "profile.normalized.json").write_text("{}\n")
    (path / "input" / "rendered-manifests.redacted.yaml").write_text("---\n")


def test_import_is_idempotent_and_exposes_summary_and_detail(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    data_dir = tmp_path / "data"
    evaluation = _evaluation()
    _write_bundle(bundle, evaluation)
    service = HistoryService.local(data_dir)

    service.import_bundle(bundle)
    service.import_bundle(bundle)

    summaries = service.list_evaluations()
    assert len(summaries) == 1
    assert summaries[0].blocker_count == 1
    assert summaries[0].failed_check_count == 1
    assert summaries[0].bundle_status == "available"
    assert service.get_evaluation(evaluation.evaluation_id) == evaluation
    assert (data_dir / "runs" / evaluation.evaluation_id / "report.md").exists()
    assert not tuple((data_dir / "runs").glob(".tmp-*"))


def test_same_evaluation_id_with_different_content_is_rejected(tmp_path: Path) -> None:
    first = tmp_path / "first"
    conflicting = tmp_path / "conflicting"
    service = HistoryService.local(tmp_path / "data")
    _write_bundle(first, _evaluation())
    _write_bundle(conflicting, _evaluation("different result"))

    service.import_bundle(first)

    with pytest.raises(HistoryError, match="different content"):
        service.import_bundle(conflicting)


def test_sync_rebuilds_projection_and_marks_missing_bundles(tmp_path: Path) -> None:
    source = tmp_path / "source"
    data_dir = tmp_path / "data"
    evaluation = _evaluation()
    _write_bundle(source, evaluation)
    initial = HistoryService.local(data_dir)
    initial.import_bundle(source)

    rebuilt = HistoryService(
        FilesystemBundleStore(data_dir / "runs"),
        SQLiteEvaluationIndex(data_dir / "rebuilt.sqlite3"),
    )
    assert rebuilt.sync() == 1
    assert rebuilt.get_evaluation(evaluation.evaluation_id) == evaluation

    stored = data_dir / "runs" / evaluation.evaluation_id
    stored.rename(data_dir / "detached-bundle")
    assert rebuilt.sync() == 0
    assert rebuilt.list_evaluations()[0].bundle_status == "missing"
    assert rebuilt.get_evaluation(evaluation.evaluation_id) is None


def test_import_rejects_incomplete_bundle(tmp_path: Path) -> None:
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "evaluation.json").write_text(_evaluation().model_dump_json())

    with pytest.raises(HistoryError, match="bundle is incomplete"):
        HistoryService.local(tmp_path / "data").import_bundle(incomplete)


def test_import_rejects_evaluation_id_as_path(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    evaluation = _evaluation().model_copy(update={"evaluation_id": "../escaped"})
    _write_bundle(bundle, evaluation)

    with pytest.raises(HistoryError, match="not a UUID"):
        HistoryService.local(tmp_path / "data").import_bundle(bundle)
    assert not (tmp_path / "escaped").exists()
