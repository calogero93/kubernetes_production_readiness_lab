"""A generic chart is reviewed before any local runtime job is dispatched."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from kubeproof.application.service import inspect_chart as real_inspect_chart
from kubeproof.core.domain import HttpProbeOptions
from kubeproof.evidence.history import HistoryService
from kubeproof.execution.helm import HelmRenderRequest, HelmRenderResult
from kubeproof.interfaces.chart_jobs import ChartJobError, ChartJobManager

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


class FakeRenderer:
    def render(self, request: HelmRenderRequest) -> HelmRenderResult:
        del request
        return HelmRenderResult(manifest=HOST_MANIFEST, stderr="")


def test_generic_chart_requires_review_and_exact_host_approval(
    tmp_path: Path,
    strict_profile_data: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kubeproof.application.service._chart_archive_version", lambda _: "1")

    def inspect(**kwargs: Any) -> Any:
        assert not kwargs.get("execute_known_chart")
        return real_inspect_chart(**kwargs, renderer=FakeRenderer())

    monkeypatch.setattr("kubeproof.interfaces.chart_jobs.inspect_chart", inspect)
    manager = ChartJobManager(HistoryService.local(tmp_path / "history"))
    dispatched: list[str] = []
    monkeypatch.setattr(manager._executor, "submit", lambda fn, job: dispatched.append(job.id))
    try:
        prepared = manager.prepare(
            {
                "chart_name": "host-reader.tgz",
                "chart_base64": base64.b64encode(b"pinned chart").decode(),
                "profile": strict_profile_data,
                "values_yaml": "",
            }
        )
        assert prepared["preview"]["admission"]["outcome"] == "static_only"
        assert prepared["approval_scope_sha256"]
        assert dispatched == []
        arguments = dict(
            chart_sha256=prepared["chart_sha256"],
            rendered_manifest_sha256=prepared["rendered_manifest_sha256"],
            approval_scope_sha256=prepared["approval_scope_sha256"],
            operator_label="lab-operator",
            approval_reason="Controlled workstation test",
        )
        with pytest.raises(ChartJobError, match="does not match"):
            manager.approve(prepared["id"], **{**arguments, "chart_sha256": "0" * 64})
        with pytest.raises(ChartJobError, match="scope does not match"):
            manager.approve(prepared["id"], **{**arguments, "approval_scope_sha256": "0" * 64})
        assert dispatched == []
        queued = manager.approve(prepared["id"], **arguments)
        assert queued["status"] == "queued"
        assert queued["operator_approval"]["reason"] == "Controlled workstation test"
        assert dispatched == [prepared["id"]]
    finally:
        manager.close()


def test_chart_job_keeps_the_http_parameters_reviewed_in_preflight(
    tmp_path: Path,
    strict_profile_data: dict[str, Any],
    http_manifest: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kubeproof.application.service._chart_archive_version", lambda _: "1")

    class Renderer:
        def render(self, request: HelmRenderRequest) -> HelmRenderResult:
            return HelmRenderResult(manifest=http_manifest, stderr="")

    options = HttpProbeOptions(service="api", port=8080, path="/ready", max_p95_ms=150)
    calls: list[bool] = []

    def inspect(**kwargs: Any) -> Any:
        assert kwargs["http_probe"] == options
        calls.append(bool(kwargs.get("execute_known_chart")))
        if kwargs.get("execute_known_chart"):
            raise RuntimeError("stop the stubbed runtime after checking reviewed parameters")
        return real_inspect_chart(**kwargs, renderer=Renderer())

    monkeypatch.setattr("kubeproof.interfaces.chart_jobs.inspect_chart", inspect)
    manager = ChartJobManager(HistoryService.local(tmp_path / "history"))
    monkeypatch.setattr(manager._executor, "submit", lambda *_: None)
    try:
        prepared = manager.prepare(
            {
                "chart_name": "api.tgz",
                "chart_base64": base64.b64encode(b"chart").decode(),
                "profile": strict_profile_data,
                "http_probe": options.model_dump(mode="json"),
            }
        )
        assert prepared["http_probe"] == options.model_dump(mode="json")
        preview_check = next(
            check
            for check in prepared["preview"]["checks"]
            if check["id"] == "runtime.http_service"
        )
        assert preview_check["assessment"] == "not_tested"
        assert calls == [False]
        manager.approve(
            prepared["id"],
            chart_sha256=prepared["chart_sha256"],
            rendered_manifest_sha256=prepared["rendered_manifest_sha256"],
            approval_scope_sha256=None,
            operator_label=None,
            approval_reason=None,
        )
        manager._run(manager._jobs[prepared["id"]])
        assert calls == [False, True]
    finally:
        manager.close()
