"""UI approval is bound to a reviewed chart and never runs during preflight."""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kubeproof.application.service import inspect_chart as real_inspect_chart
from kubeproof.evidence.history import HistoryService
from kubeproof.execution.helm import HelmRenderRequest, HelmRenderResult
from kubeproof.intelligence.models import ConfirmedRequest, CpuGoal, RunBudget
from kubeproof.interfaces.live_jobs import LiveJobError, LiveJobManager


def confirmed() -> ConfirmedRequest:
    return ConfirmedRequest(
        environment_id="local-kind",
        target_id="cpu-fixture",
        work_iterations=20_000,
        goal=CpuGoal(
            target_rps=5,
            max_p95_ms=500,
            max_failed_requests=0,
            max_cpu_millicores=500,
        ),
        budget=RunBudget(
            max_plan_versions=1,
            max_elapsed_seconds=1200,
            max_model_calls=1,
            max_tool_calls=3,
            max_requests_per_trial=10_000,
        ),
    )


MANIFEST = b"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kubeproof-target
  namespace: kubeproof-product
  labels: {app.kubernetes.io/name: cpu-fixture, app.kubernetes.io/instance: kubeproof-target}
spec:
  replicas: 2
  selector:
    matchLabels: {app.kubernetes.io/name: cpu-fixture, app.kubernetes.io/instance: kubeproof-target}
  template:
    metadata:
      labels: {app.kubernetes.io/name: cpu-fixture, app.kubernetes.io/instance: kubeproof-target}
    spec:
      securityContext: {runAsNonRoot: true, runAsUser: 65532, runAsGroup: 65532}
      containers:
      - name: cpu-fixture
        image: kubeproof-cpu-fixture:local
        imagePullPolicy: IfNotPresent
        args: [--host, 0.0.0.0, --iterations, "20000"]
        ports: [{name: http, containerPort: 8080}]
        readinessProbe: {httpGet: {path: /healthz, port: http}}
        livenessProbe: {httpGet: {path: /healthz, port: http}}
        securityContext:
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities: {drop: [ALL]}
        resources:
          requests: {cpu: 100m, memory: 32Mi}
          limits: {cpu: 500m, memory: 128Mi}
---
apiVersion: v1
kind: Service
metadata:
  name: kubeproof-target
  namespace: kubeproof-product
  labels: {app.kubernetes.io/name: cpu-fixture, app.kubernetes.io/instance: kubeproof-target}
spec:
  type: ClusterIP
  selector: {app.kubernetes.io/name: cpu-fixture, app.kubernetes.io/instance: kubeproof-target}
  ports: [{name: http, port: 8080, targetPort: http}]
"""


class FakeRenderer:
    def render(self, request: HelmRenderRequest) -> HelmRenderResult:
        del request
        return HelmRenderResult(manifest=MANIFEST, stderr="")


def submission(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "chart_name": "cpu-fixture.tgz",
        "chart_base64": base64.b64encode(b"test archive").decode("ascii"),
        "profile": profile,
        "request": confirmed().model_dump(mode="json"),
        "use_ai": False,
    }


def test_preflight_and_exact_approval_do_not_execute_before_approval(
    tmp_path: Path,
    strict_profile_data: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kubeproof.application.service._chart_archive_version", lambda _: "0.1.0")

    def inspect(**kwargs: Any) -> Any:
        assert kwargs["execute_known_chart"] is False
        return real_inspect_chart(**kwargs, renderer=FakeRenderer())

    monkeypatch.setattr("kubeproof.interfaces.live_jobs.inspect_chart", inspect)
    history = HistoryService.local(tmp_path / "history")
    manager = LiveJobManager(history)
    dispatched: list[str] = []
    monkeypatch.setattr(manager._executor, "submit", lambda fn, job: dispatched.append(job.id))
    try:
        prepared = manager.prepare(submission(strict_profile_data))
        assert prepared["status"] == "prepared"
        assert prepared["load_error"] is None
        assert prepared["preview"]["admission"]["outcome"] == "admit"
        assert dispatched == []
        with pytest.raises(LiveJobError, match="does not match"):
            manager.approve(prepared["id"], "0" * 64)
        queued = manager.approve(prepared["id"], prepared["chart_sha256"])
        assert queued["status"] == "queued"
        assert dispatched == [prepared["id"]]
        with pytest.raises(LiveJobError, match="no longer awaiting"):
            manager.approve(prepared["id"], prepared["chart_sha256"])
    finally:
        manager.close()


def test_invalid_chart_or_wrong_environment_is_rejected_before_live_execution(
    tmp_path: Path, strict_profile_data: dict[str, Any]
) -> None:
    manager = LiveJobManager(HistoryService.local(tmp_path / "history"))
    try:
        payload = submission(strict_profile_data)
        payload["chart_name"] = "../unsafe.tgz"
        with pytest.raises(LiveJobError, match="without path components"):
            manager.prepare(payload)
        payload = submission(strict_profile_data)
        payload["request"]["environment_id"] = "production"
        with pytest.raises(LiveJobError, match="local-kind"):
            manager.prepare(payload)
        payload = submission(strict_profile_data)
        payload["chart_base64"] = "not-base64!"
        with pytest.raises(LiveJobError, match="valid base64"):
            manager.prepare(payload)
        payload = submission(strict_profile_data)
        payload["request"]["budget"]["max_plan_versions"] = 2
        with pytest.raises(LiveJobError, match="pilot mode"):
            manager.prepare(payload)
    finally:
        manager.close()


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is not installed")
def test_packaged_fixture_passes_real_preflight(
    tmp_path: Path, strict_profile_data: dict[str, Any]
) -> None:
    chart_root = Path(__file__).resolve().parents[2] / "examples/charts/cpu-fixture"
    subprocess.run(
        ["helm", "package", str(chart_root), "--destination", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    archive = tmp_path / "cpu-fixture-0.1.0.tgz"
    payload = submission(strict_profile_data)
    payload["chart_name"] = archive.name
    payload["chart_base64"] = base64.b64encode(archive.read_bytes()).decode("ascii")
    manager = LiveJobManager(HistoryService.local(tmp_path / "history"))
    try:
        prepared = manager.prepare(payload)
        assert prepared["status"] == "prepared"
        assert prepared["preview"]["admission"]["outcome"] == "admit"
        assert prepared["load_error"] is None
    finally:
        manager.close()
