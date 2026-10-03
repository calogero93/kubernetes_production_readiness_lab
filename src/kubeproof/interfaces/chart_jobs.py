"""Local review and execution of a selected generic Helm chart."""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
import tempfile
import threading
import uuid
from _thread import LockType
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kubeproof.application.service import inspect_chart
from kubeproof.core.domain import AdmissionOutcome, HttpProbeOptions, OperatorApproval
from kubeproof.core.plans import EvaluationPlan, PlanBudget
from kubeproof.core.profile import CompanyProfile
from kubeproof.evidence.history import HistoryService
from kubeproof.observability import LIVE_RUNS

MAX_CHART_BYTES = 10 * 1024 * 1024
MAX_VALUES_BYTES = 256 * 1024


class ChartJobError(ValueError):
    """The submitted chart or approval does not match the reviewed input."""


class ChartSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chart_name: str = Field(min_length=1)
    chart_base64: str = Field(min_length=1)
    profile: dict[str, Any]
    values_yaml: str = ""
    http_probe: HttpProbeOptions | None = None
    test_plan: EvaluationPlan | None = None
    ai_plan: bool = False
    objective: str | None = Field(default=None, max_length=4000)
    plan_budget: PlanBudget = PlanBudget()
    ai_interpret: bool = False


@dataclass
class _ChartJob:
    id: str
    directory: tempfile.TemporaryDirectory[str]
    chart: Path
    values: Path | None
    profile: CompanyProfile
    preview: dict[str, Any]
    chart_sha256: str
    rendered_manifest_sha256: str
    approval_scope_sha256: str | None
    http_probe: HttpProbeOptions | None
    test_plan: EvaluationPlan
    ai_interpret: bool
    status: str = "prepared"
    operator_approval: OperatorApproval | None = None
    evaluation_id: str | None = None
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "chart_name": self.chart.name,
            "chart_sha256": self.chart_sha256,
            "rendered_manifest_sha256": self.rendered_manifest_sha256,
            "approval_scope_sha256": self.approval_scope_sha256,
            "http_probe": self.http_probe.model_dump(mode="json") if self.http_probe else None,
            "test_plan": self.test_plan.model_dump(mode="json"),
            "plan_sha256": self.test_plan.digest(),
            "ai_interpret": self.ai_interpret,
            "preview": self.preview,
            "operator_approval": (
                self.operator_approval.model_dump(mode="json") if self.operator_approval else None
            ),
            "evaluation_id": self.evaluation_id,
            "error": self.error,
        }


class ChartJobManager:
    """One reviewed chart run at a time, with an exact local approval boundary."""

    def __init__(self, history: HistoryService, run_lock: LockType | None = None):
        self.history = history
        self.csrf_token = secrets.token_urlsafe(32)
        self._jobs: dict[str, _ChartJob] = {}
        self._lock = threading.Lock()
        self._run_lock = run_lock or threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kubeproof-chart")

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            for job in self._jobs.values():
                if job.status not in {"queued", "running"}:
                    job.directory.cleanup()

    def prepare(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            submission = ChartSubmission.model_validate(raw)
            profile = CompanyProfile.model_validate(submission.profile)
        except ValidationError as exc:
            raise ChartJobError(f"invalid chart submission or profile: {exc}") from exc
        if (
            not submission.chart_name.endswith(".tgz")
            or Path(submission.chart_name).name != submission.chart_name
        ):
            raise ChartJobError("chart must be one .tgz file without path components")
        if len(submission.chart_base64) > (MAX_CHART_BYTES * 4 // 3) + 8:
            raise ChartJobError("chart archive exceeds the 10 MiB upload limit")
        try:
            chart_bytes = base64.b64decode(submission.chart_base64, validate=True)
        except binascii.Error as exc:
            raise ChartJobError("chart is not valid base64") from exc
        if not chart_bytes or len(chart_bytes) > MAX_CHART_BYTES:
            raise ChartJobError("chart archive must be nonempty and at most 10 MiB")
        values_bytes = submission.values_yaml.encode("utf-8")
        if len(values_bytes) > MAX_VALUES_BYTES:
            raise ChartJobError("values YAML exceeds the 256 KiB limit")

        intelligence = None
        if submission.ai_plan or submission.ai_interpret:
            from kubeproof.intelligence.evaluation import EvaluationAI

            intelligence = EvaluationAI()
        directory = tempfile.TemporaryDirectory(prefix="kubeproof-chart-")
        root = Path(directory.name)
        chart = root / submission.chart_name
        chart.write_bytes(chart_bytes)
        values = root / "reviewed-values.yaml" if values_bytes.strip() else None
        if values is not None:
            values.write_bytes(values_bytes)
        try:
            evaluation = inspect_chart(
                chart=str(chart),
                version=None,
                profile=profile,
                values_files=(values,) if values is not None else (),
                set_values=(),
                output=root / "preview",
                http_probe=submission.http_probe,
                test_plan=submission.test_plan,
                plan_generator=intelligence.propose
                if submission.ai_plan and intelligence
                else None,
                planning_objective=submission.objective,
                plan_budget=submission.plan_budget,
            )
        except Exception:
            directory.cleanup()
            raise
        assert evaluation.execution_options.test_plan is not None
        job = _ChartJob(
            id=str(uuid.uuid4()),
            directory=directory,
            chart=chart,
            values=values,
            profile=profile,
            preview=evaluation.model_dump(mode="json"),
            chart_sha256=hashlib.sha256(chart_bytes).hexdigest(),
            rendered_manifest_sha256=evaluation.input.rendered_manifest_sha256,
            approval_scope_sha256=evaluation.admission.approval_scope_sha256,
            http_probe=submission.http_probe,
            test_plan=evaluation.execution_options.test_plan,
            ai_interpret=submission.ai_interpret,
        )
        with self._lock:
            self._jobs[job.id] = job
        return job.snapshot()

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                return self._jobs[job_id].snapshot()
            except KeyError as exc:
                raise ChartJobError("chart run not found") from exc

    def approve(
        self,
        job_id: str,
        *,
        chart_sha256: str,
        rendered_manifest_sha256: str,
        approval_scope_sha256: str | None,
        operator_label: str | None,
        approval_reason: str | None,
        plan_sha256: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError as exc:
                raise ChartJobError("chart run not found") from exc
            if job.status != "prepared":
                raise ChartJobError("chart run is no longer awaiting approval")
            if any(other.status in {"queued", "running"} for other in self._jobs.values()):
                raise ChartJobError("another chart run is already queued or running")
            if (
                chart_sha256 != job.chart_sha256
                or rendered_manifest_sha256 != job.rendered_manifest_sha256
            ):
                raise ChartJobError("approval does not match the reviewed chart and manifest")
            if plan_sha256 is not None and plan_sha256 != job.test_plan.digest():
                raise ChartJobError("approval does not match the reviewed plan")
            admission = job.preview["admission"]
            if admission["outcome"] == AdmissionOutcome.STATIC_ONLY.value:
                if not job.approval_scope_sha256:
                    raise ChartJobError(
                        "this chart requires safety rules that cannot be approved locally"
                    )
                if approval_scope_sha256 != job.approval_scope_sha256:
                    raise ChartJobError("host-access approval scope does not match the preflight")
                try:
                    job.operator_approval = OperatorApproval(
                        operator_label=operator_label or "",
                        reason=approval_reason or "",
                        scope_sha256=job.approval_scope_sha256,
                        approved_at=datetime.now(UTC),
                    )
                except ValidationError as exc:
                    raise ChartJobError(f"invalid operator approval: {exc}") from exc
            elif admission["outcome"] != AdmissionOutcome.ADMIT.value:
                raise ChartJobError("static admission did not permit a live run")
            elif approval_scope_sha256 or operator_label or approval_reason:
                raise ChartJobError("host-access approval was supplied for an admitted chart")
            job.status = "queued"
            self._executor.submit(self._run, job)
            return job.snapshot()

    def _run(self, job: _ChartJob) -> None:
        with self._run_lock:
            with self._lock:
                job.status = "running"
            try:
                intelligence = None
                if job.ai_interpret:
                    from kubeproof.intelligence.evaluation import EvaluationAI

                    intelligence = EvaluationAI()
                evaluation = inspect_chart(
                    chart=str(job.chart),
                    version=None,
                    profile=job.profile,
                    values_files=(job.values,) if job.values is not None else (),
                    set_values=(),
                    output=Path(job.directory.name) / "final",
                    execute_known_chart=True,
                    operator_approval=job.operator_approval,
                    expected_rendered_manifest_sha256=job.rendered_manifest_sha256,
                    http_probe=job.http_probe,
                    test_plan=job.test_plan,
                    expected_plan_sha256=job.test_plan.digest(),
                    interpreter=intelligence.interpret if intelligence else None,
                )
                self.history.import_bundle(Path(job.directory.name) / "final")
            except Exception as exc:
                with self._lock:
                    job.status = "error"
                    job.error = str(exc)
                LIVE_RUNS.labels("error").inc()
            else:
                with self._lock:
                    job.status = "completed"
                    job.evaluation_id = evaluation.evaluation_id
                LIVE_RUNS.labels("completed").inc()
            finally:
                job.directory.cleanup()
