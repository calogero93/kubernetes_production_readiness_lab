"""Opt-in real Docker/kind smoke test for the approved CPU UI path."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from kubeproof.evidence.history import HistoryService
from kubeproof.intelligence.models import ConfirmedRequest, CpuGoal, RunBudget
from kubeproof.interfaces.live_jobs import LiveJobManager


@pytest.mark.skipif(
    os.environ.get("KUBEPROOF_RUN_LIVE_SMOKE") != "1"
    or any(shutil.which(name) is None for name in ("docker", "kind", "kubectl", "helm")),
    reason="opt-in Docker/kind smoke test",
)
def test_live_cpu_workflow_from_packaged_chart(
    tmp_path: Path, strict_profile_data: dict[str, Any]
) -> None:
    chart_root = Path(__file__).resolve().parents[2] / "examples/charts/cpu-fixture"
    subprocess.run(
        ["helm", "package", str(chart_root), "--destination", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    archive = tmp_path / "cpu-fixture-0.1.0.tgz"
    request = ConfirmedRequest(
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
    history = HistoryService.local(tmp_path / "history")
    manager = LiveJobManager(history)
    try:
        prepared = manager.prepare(
            {
                "chart_name": archive.name,
                "chart_base64": base64.b64encode(archive.read_bytes()).decode("ascii"),
                "profile": strict_profile_data,
                "request": request.model_dump(mode="json"),
            }
        )
        assert prepared["load_error"] is None
        manager.approve(prepared["id"], prepared["chart_sha256"])
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            job = manager.get(prepared["id"])
            if job["status"] in {"completed", "error"}:
                break
            time.sleep(3)
        else:
            pytest.fail("live CPU job exceeded the 15-minute smoke-test deadline")
        assert job["status"] == "completed", job["error"]
        evaluation = history.get_evaluation(job["evaluation_id"])
        assert evaluation is not None
        checks = {check.id: check for check in evaluation.checks}
        assert checks["runtime.installation"].assessment == "pass"
        assert checks["runtime.cpu_load"].assessment in {"pass", "inconclusive"}
        assert any(
            item.observation_type == "cpu.bounded_load_trial" for item in evaluation.observations
        )
        assert evaluation.environment is not None
        assert evaluation.environment.cleanup_succeeded
    finally:
        manager.close()
