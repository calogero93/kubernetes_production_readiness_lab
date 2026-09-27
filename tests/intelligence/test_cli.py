"""AI CLI commands preview drafts/plans without invoking trial tools."""

from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from typer.testing import CliRunner

from kubeproof.intelligence.model_config import LocalModelStatus, ModelProbeError
from kubeproof.intelligence.models import RequestDraft, TestPlan
from kubeproof.interfaces.cli import app

from .test_control import request, task


def test_schema_is_available_without_model_configuration() -> None:
    result = CliRunner().invoke(app, ["ai", "schema"])
    assert result.exit_code == 0
    assert "max_cpu_millicores" in result.stdout


def test_doctor_reports_local_model_without_running_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUBEPROOF_AI_PROVIDER", "llama_cpp")
    monkeypatch.setattr(
        "kubeproof.intelligence.model_config.probe_llama_cpp",
        lambda: LocalModelStatus("http://127.0.0.1:8080/v1", "selected-gguf"),
    )
    result = CliRunner().invoke(app, ["ai", "doctor"])
    assert result.exit_code == 0
    assert "selected-gguf" in result.stdout


def test_doctor_fails_when_local_server_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBEPROOF_AI_PROVIDER", "llama_cpp")

    def unavailable() -> None:
        raise ModelProbeError("cannot connect to the local model server")

    monkeypatch.setattr("kubeproof.intelligence.model_config.probe_llama_cpp", unavailable)
    result = CliRunner().invoke(app, ["ai", "doctor"])
    assert result.exit_code == 1
    assert "cannot connect" in result.output


def test_draft_is_unconfirmed_and_does_not_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = RequestDraft(
        environment_id="sandbox-1",
        target_id="cpu-fixture",
        source_fragments={"environment_id": "sandbox-1", "target_id": "cpu-fixture"},
    )
    fake = FakeListChatModel(responses=[draft.model_dump_json()])
    monkeypatch.setattr("kubeproof.intelligence.model_config.create_chat_model", lambda: fake)
    result = CliRunner().invoke(app, ["ai", "draft", "Evaluate cpu-fixture in sandbox-1"])
    assert result.exit_code == 0
    assert '"environment_id": "sandbox-1"' in result.stdout
    assert '"target_rps": null' in result.stdout


def test_plan_previews_validated_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    request_path = tmp_path / "confirmed.json"
    request_path.write_text(request().model_dump_json())
    plan = TestPlan(version=1, tasks=(task("cpu-50", 50),))
    fake = FakeListChatModel(responses=[plan.model_dump_json()])
    monkeypatch.setattr("kubeproof.intelligence.model_config.create_chat_model", lambda: fake)
    result = CliRunner().invoke(app, ["ai", "plan", str(request_path)])
    assert result.exit_code == 0
    assert '"id": "cpu-50"' in result.stdout
