"""Local-only UI workflow for reviewed, bounded CPU experiments."""

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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kubeproof.application.service import inspect_chart
from kubeproof.core.domain import AdmissionOutcome
from kubeproof.core.manifests import parse_manifests
from kubeproof.core.plans import EvaluationPlan
from kubeproof.core.profile import CompanyProfile
from kubeproof.evidence.history import HistoryService
from kubeproof.execution.cpu_fixture import validate_fixture_resources
from kubeproof.execution.cpu_live import CpuLiveExperiment
from kubeproof.execution.helm import HelmRenderRequest
from kubeproof.intelligence.models import ConfirmedRequest
from kubeproof.observability import LIVE_RUNS

MAX_CHART_BYTES = 10 * 1024 * 1024


class LiveJobError(ValueError):
    """Invalid input or transition in the local live-run workflow."""


class LiveSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chart_name: str = Field(min_length=1)
    chart_base64: str = Field(min_length=1)
    profile: dict[str, Any]
    request: dict[str, Any]
    use_ai: bool = False


def _validated_submission(
    raw: dict[str, Any],
) -> tuple[LiveSubmission, CompanyProfile, ConfirmedRequest, bytes]:
    """Validate the reviewed input before allocating a directory or starting a run."""
    try:
        submission = LiveSubmission.model_validate(raw)
        profile = CompanyProfile.model_validate(submission.profile)
        request = ConfirmedRequest.model_validate(submission.request)
    except ValidationError as exc:
        raise LiveJobError(f"invalid profile or confirmed requirements: {exc}") from exc
    if request.environment_id != "local-kind" or request.target_id != "cpu-fixture":
        raise LiveJobError(
            "first live run requires environment_id=local-kind and target_id=cpu-fixture"
        )
    if (
        not submission.chart_name.endswith(".tgz")
        or Path(submission.chart_name).name != submission.chart_name
    ):
        raise LiveJobError("chart must be one .tgz file without path components")
    if len(submission.chart_base64) > (MAX_CHART_BYTES * 4 // 3) + 8:
        raise LiveJobError("chart archive exceeds the 10 MiB upload limit")
    try:
        chart_bytes = base64.b64decode(submission.chart_base64, validate=True)
    except binascii.Error as exc:
        raise LiveJobError("chart is not valid base64") from exc
    if not chart_bytes or len(chart_bytes) > MAX_CHART_BYTES:
        raise LiveJobError("chart archive must be nonempty and at most 10 MiB")
    if not submission.use_ai and request.budget.max_plan_versions != 1:
        raise LiveJobError("pilot mode requires max_plan_versions=1")
    if submission.use_ai:
        CpuLiveExperiment(request, use_ai=True)  # Fail before approval if model is unconfigured.
    return submission, profile, request, chart_bytes


def _fixture_settings(request: ConfirmedRequest) -> tuple[str, ...]:
    return (
        "image.repository=kubeproof-cpu-fixture",
        "image.tag=local",
        f"workIterations={request.work_iterations}",
    )


def _preview_chart(
    chart: Path,
    profile: CompanyProfile,
    request: ConfirmedRequest,
    output: Path,
    *,
    use_ai: bool = False,
) -> tuple[dict[str, Any], str | None]:
    settings = _fixture_settings(request)
    static_output = output.with_name(output.name + "-static")
    evaluation = inspect_chart(
        chart=str(chart),
        version=None,
        profile=profile,
        values_files=(),
        set_values=settings,
        output=static_output,
        execute_known_chart=False,
    )
    rendered = (static_output / "input" / "rendered-manifests.redacted.yaml").read_bytes()
    load_error: str | None
    try:
        validate_fixture_resources(
            parse_manifests(rendered),
            HelmRenderRequest(chart=str(chart), set_values=settings),
            request,
        )
    except ValueError as exc:
        load_error = str(exc)
    else:
        load_error = None
        plan = CpuLiveExperiment(request, use_ai=use_ai).baseline_plan(
            parse_manifests(rendered),
            HelmRenderRequest(chart=str(chart), set_values=settings),
            profile,
        )
        evaluation = inspect_chart(
            chart=str(chart),
            version=None,
            profile=profile,
            values_files=(),
            set_values=settings,
            output=output,
            execute_known_chart=False,
            test_plan=plan,
        )
    return evaluation.model_dump(mode="json"), load_error


@dataclass
class _Job:
    id: str
    directory: tempfile.TemporaryDirectory[str]
    chart: Path
    chart_sha256: str
    profile: CompanyProfile
    request: ConfirmedRequest
    use_ai: bool
    preview: dict[str, Any]
    load_error: str | None = None
    status: str = "prepared"
    evaluation_id: str | None = None
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "chart_name": self.chart.name,
            "chart_sha256": self.chart_sha256,
            "preview": self.preview,
            "load_error": self.load_error,
            "use_ai": self.use_ai,
            "profile_name": self.profile.name,
            "confirmed_request": self.request.model_dump(mode="json"),
            "evaluation_id": self.evaluation_id,
            "error": self.error,
        }


class LiveJobManager:
    """One local run at a time; the final sealed bundle is stored in history."""

    def __init__(self, history: HistoryService, run_lock: LockType | None = None):
        self.history = history
        self.csrf_token = secrets.token_urlsafe(32)
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._run_lock = run_lock or threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kubeproof-live")

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            for job in self._jobs.values():
                if job.status not in {"queued", "running"}:
                    job.directory.cleanup()

    def prepare(self, raw: dict[str, Any]) -> dict[str, Any]:
        submission, profile, request, chart_bytes = _validated_submission(raw)

        directory = tempfile.TemporaryDirectory(prefix="kubeproof-live-")
        root = Path(directory.name)
        chart = root / submission.chart_name
        chart.write_bytes(chart_bytes)
        try:
            preview, load_error = _preview_chart(
                chart, profile, request, root / "preview", use_ai=submission.use_ai
            )
        except Exception:
            directory.cleanup()
            raise
        chart_sha256 = hashlib.sha256(chart_bytes).hexdigest()
        job = _Job(
            id=str(uuid.uuid4()),
            directory=directory,
            chart=chart,
            chart_sha256=chart_sha256,
            profile=profile,
            request=request,
            use_ai=submission.use_ai,
            preview=preview,
            load_error=load_error,
        )
        with self._lock:
            self._jobs[job.id] = job
        return job.snapshot()

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                return self._jobs[job_id].snapshot()
            except KeyError as exc:
                raise LiveJobError("live run not found") from exc

    def approve(self, job_id: str, expected_sha256: str) -> dict[str, Any]:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError as exc:
                raise LiveJobError("live run not found") from exc
            if job.status != "prepared":
                raise LiveJobError("live run is no longer awaiting approval")
            if any(
                other.status in {"queued", "running"}
                for other in self._jobs.values()
                if other.id != job.id
            ):
                raise LiveJobError("another live run is already queued or running")
            if job.chart_sha256 != expected_sha256:
                raise LiveJobError("approval does not match the reviewed chart")
            if job.preview["admission"]["outcome"] != AdmissionOutcome.ADMIT.value:
                raise LiveJobError("static safety admission did not permit a live run")
            if job.load_error is not None:
                raise LiveJobError(job.load_error)
            job.status = "queued"
            self._executor.submit(self._run, job)
            return job.snapshot()

    def _run(self, job: _Job) -> None:
        with self._run_lock:
            self._run_exclusive(job)

    def _run_exclusive(self, job: _Job) -> None:
        with self._lock:
            job.status = "running"
        try:
            intelligence = None
            if job.use_ai:
                from kubeproof.intelligence.evaluation import EvaluationAI

                intelligence = EvaluationAI()
            evaluation = inspect_chart(
                chart=str(job.chart),
                version=None,
                profile=job.profile,
                values_files=(),
                set_values=_fixture_settings(job.request),
                output=Path(job.directory.name) / "final",
                execute_known_chart=True,
                install_timeout_seconds=300,
                steady_state_seconds=15,
                max_recovery_targets=0,
                test_plan=EvaluationPlan.model_validate(
                    job.preview["execution_options"]["test_plan"]
                ),
                expected_plan_sha256=EvaluationPlan.model_validate(
                    job.preview["execution_options"]["test_plan"]
                ).digest(),
                expected_rendered_manifest_sha256=job.preview["input"]["rendered_manifest_sha256"],
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
