"""Local immutable bundle storage and a rebuildable SQLite history index."""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from kubeproof.bundle import BundleError, verify_bundle
from kubeproof.domain import Assessment, EvaluationResult, Severity


class HistoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class BundleRef:
    evaluation_id: str
    relative_path: str
    evaluation_sha256: str


@dataclass(frozen=True)
class EvaluationSummary:
    evaluation_id: str
    generated_at: str
    chart: str
    requested_version: str | None
    profile_name: str
    admission_outcome: str
    blocker_count: int
    warning_count: int
    failed_check_count: int
    warning_check_count: int
    bundle_status: str


class BundleStore(Protocol):
    def put(self, source: Path) -> tuple[BundleRef, EvaluationResult]: ...

    def discover(self) -> tuple[tuple[BundleRef, EvaluationResult], ...]: ...

    def load(self, reference: BundleRef) -> EvaluationResult: ...


class EvaluationIndex(Protocol):
    def upsert(self, reference: BundleRef, evaluation: EvaluationResult) -> None: ...

    def mark_missing_except(self, evaluation_ids: set[str]) -> None: ...

    def list(self) -> tuple[EvaluationSummary, ...]: ...

    def get_reference(self, evaluation_id: str) -> BundleRef | None: ...


def _load_evaluation(bundle: Path) -> tuple[EvaluationResult, str, str | None]:
    try:
        verified = verify_bundle(bundle)
    except BundleError as exc:
        raise HistoryError(str(exc)) from exc
    return verified.evaluation, verified.evaluation_sha256, verified.bundle_sha256


def _canonical_evaluation_id(value: str) -> str:
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise HistoryError(f"evaluation id is not a UUID: {value}") from exc
    if canonical != value:
        raise HistoryError(f"evaluation id is not canonical: {value}")
    return canonical


class FilesystemBundleStore:
    """Store each canonical bundle under its immutable evaluation identifier."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, source: Path) -> tuple[BundleRef, EvaluationResult]:
        evaluation, digest, bundle_digest = _load_evaluation(source)
        evaluation_id = _canonical_evaluation_id(evaluation.evaluation_id)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / evaluation_id
        reference = BundleRef(evaluation_id, evaluation_id, digest)

        if target.exists():
            existing, existing_digest, existing_bundle_digest = _load_evaluation(target)
            if existing_digest != digest or existing_bundle_digest != bundle_digest:
                raise HistoryError(
                    f"evaluation id {evaluation.evaluation_id} already exists "
                    "with different content"
                )
            existing_reference = BundleRef(
                existing.evaluation_id, existing.evaluation_id, existing_digest
            )
            return existing_reference, existing

        temporary = self.root / f".tmp-{evaluation.evaluation_id}-{uuid.uuid4().hex}"
        try:
            shutil.copytree(source, temporary, symlinks=True)
            copied, copied_digest, copied_bundle_digest = _load_evaluation(temporary)
            if copied_digest != digest or copied_bundle_digest != bundle_digest:
                raise HistoryError("bundle changed while it was being imported")
            temporary.replace(target)
            return reference, copied
        except (OSError, shutil.Error) as exc:
            raise HistoryError(f"cannot import bundle {source}: {exc}") from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def discover(self) -> tuple[tuple[BundleRef, EvaluationResult], ...]:
        if not self.root.exists():
            return ()
        discovered: list[tuple[BundleRef, EvaluationResult]] = []
        for bundle in sorted(self.root.iterdir()):
            if not bundle.is_dir() or bundle.name.startswith("."):
                continue
            evaluation, digest, _ = _load_evaluation(bundle)
            evaluation_id = _canonical_evaluation_id(evaluation.evaluation_id)
            if bundle.name != evaluation_id:
                raise HistoryError(
                    f"bundle directory {bundle.name} does not match {evaluation.evaluation_id}"
                )
            discovered.append((BundleRef(evaluation_id, bundle.name, digest), evaluation))
        return tuple(discovered)

    def load(self, reference: BundleRef) -> EvaluationResult:
        evaluation_id = _canonical_evaluation_id(reference.evaluation_id)
        if reference.relative_path != evaluation_id:
            raise HistoryError("bundle reference path does not match its evaluation id")
        bundle = self.root / evaluation_id
        evaluation, digest, _ = _load_evaluation(bundle)
        if evaluation.evaluation_id != evaluation_id:
            raise HistoryError("stored bundle evaluation id does not match its reference")
        if digest != reference.evaluation_sha256:
            raise HistoryError(
                f"evaluation bundle {evaluation.evaluation_id} failed digest validation"
            )
        return evaluation


class SQLiteEvaluationIndex:
    """Query projection. All rows can be reconstructed from the bundle store."""

    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    generated_at TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    tool_version TEXT NOT NULL,
                    chart TEXT NOT NULL,
                    requested_version TEXT,
                    profile_name TEXT NOT NULL,
                    admission_outcome TEXT NOT NULL,
                    blocker_count INTEGER NOT NULL,
                    warning_count INTEGER NOT NULL,
                    failed_check_count INTEGER NOT NULL,
                    warning_check_count INTEGER NOT NULL,
                    bundle_path TEXT NOT NULL,
                    evaluation_sha256 TEXT NOT NULL,
                    bundle_status TEXT NOT NULL CHECK (bundle_status IN ('available', 'missing')),
                    indexed_at TEXT NOT NULL
                )
                """
            )

    def upsert(self, reference: BundleRef, evaluation: EvaluationResult) -> None:
        _canonical_evaluation_id(evaluation.evaluation_id)
        severities = Counter(finding.severity for finding in evaluation.findings)
        assessments = Counter(check.assessment for check in evaluation.checks)
        values = (
            evaluation.evaluation_id,
            evaluation.generated_at.isoformat(),
            evaluation.schema_version,
            evaluation.tool_version,
            evaluation.input.chart,
            evaluation.input.requested_version,
            evaluation.profile_name,
            evaluation.admission.outcome.value,
            severities[Severity.BLOCKER],
            severities[Severity.WARNING],
            assessments[Assessment.FAIL],
            assessments[Assessment.WARNING],
            reference.relative_path,
            reference.evaluation_sha256,
            "available",
            datetime.now(UTC).isoformat(),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO evaluations (
                    evaluation_id, generated_at, schema_version, tool_version, chart,
                    requested_version, profile_name, admission_outcome, blocker_count,
                    warning_count, failed_check_count, warning_check_count, bundle_path,
                    evaluation_sha256, bundle_status, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evaluation_id) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    schema_version = excluded.schema_version,
                    tool_version = excluded.tool_version,
                    chart = excluded.chart,
                    requested_version = excluded.requested_version,
                    profile_name = excluded.profile_name,
                    admission_outcome = excluded.admission_outcome,
                    blocker_count = excluded.blocker_count,
                    warning_count = excluded.warning_count,
                    failed_check_count = excluded.failed_check_count,
                    warning_check_count = excluded.warning_check_count,
                    bundle_path = excluded.bundle_path,
                    evaluation_sha256 = excluded.evaluation_sha256,
                    bundle_status = 'available',
                    indexed_at = excluded.indexed_at
                """,
                values,
            )

    def mark_missing_except(self, evaluation_ids: set[str]) -> None:
        with self._connect() as connection:
            rows = connection.execute("SELECT evaluation_id FROM evaluations").fetchall()
            missing = [
                row["evaluation_id"] for row in rows if row["evaluation_id"] not in evaluation_ids
            ]
            connection.executemany(
                "UPDATE evaluations SET bundle_status = 'missing' WHERE evaluation_id = ?",
                ((evaluation_id,) for evaluation_id in missing),
            )

    def list(self) -> tuple[EvaluationSummary, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT evaluation_id, generated_at, chart, requested_version, profile_name,
                       admission_outcome, blocker_count, warning_count, failed_check_count,
                       warning_check_count, bundle_status
                FROM evaluations
                ORDER BY generated_at DESC, evaluation_id DESC
                """
            ).fetchall()
        return tuple(EvaluationSummary(**dict(row)) for row in rows)

    def get_reference(self, evaluation_id: str) -> BundleRef | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT evaluation_id, bundle_path, evaluation_sha256, bundle_status
                FROM evaluations WHERE evaluation_id = ?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None or row["bundle_status"] != "available":
            return None
        return BundleRef(row["evaluation_id"], row["bundle_path"], row["evaluation_sha256"])


class HistoryService:
    def __init__(self, bundle_store: BundleStore, index: EvaluationIndex) -> None:
        self.bundle_store = bundle_store
        self.index = index

    @classmethod
    def local(cls, data_dir: Path) -> HistoryService:
        return cls(
            FilesystemBundleStore(data_dir / "runs"),
            SQLiteEvaluationIndex(data_dir / "history.sqlite3"),
        )

    def import_bundle(self, source: Path) -> EvaluationResult:
        reference, evaluation = self.bundle_store.put(source)
        self.index.upsert(reference, evaluation)
        return evaluation

    def sync(self) -> int:
        discovered = self.bundle_store.discover()
        ids: set[str] = set()
        for reference, evaluation in discovered:
            self.index.upsert(reference, evaluation)
            ids.add(evaluation.evaluation_id)
        self.index.mark_missing_except(ids)
        return len(discovered)

    def list_evaluations(self) -> tuple[EvaluationSummary, ...]:
        return self.index.list()

    def get_evaluation(self, evaluation_id: str) -> EvaluationResult | None:
        reference = self.index.get_reference(evaluation_id)
        if reference is None:
            return None
        return self.bundle_store.load(reference)
