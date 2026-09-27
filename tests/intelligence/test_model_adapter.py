"""Model selection is dynamic; neither tests nor the code pin a real LLM."""

import json
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from kubeproof.intelligence.capabilities import cpu_capabilities
from kubeproof.intelligence.model_adapter import (
    InvalidModelPlan,
    InvalidRequirementDraft,
    LangChainRequirementsExtractor,
    LangChainSupervisor,
)
from kubeproof.intelligence.model_config import (
    ModelConfigurationError,
    ModelProbeError,
    create_chat_model,
    probe_llama_cpp,
)
from kubeproof.intelligence.models import RequestDraft, TestPlan

from .test_control import request, task


def test_local_openai_compatible_endpoint_is_configurable() -> None:
    model = create_chat_model(
        {
            "KUBEPROOF_AI_PROVIDER": "openai_compatible",
            "KUBEPROOF_AI_MODEL": "local-model",
            "KUBEPROOF_AI_BASE_URL": "http://127.0.0.1:1234/v1",
        }
    )
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "local-model"


def test_llama_cpp_uses_loopback_default_without_pinning_model() -> None:
    model = create_chat_model(
        {"KUBEPROOF_AI_PROVIDER": "llama_cpp", "KUBEPROOF_AI_MODEL": "selected-gguf"}
    )
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "selected-gguf"
    assert str(model.openai_api_base) == "http://127.0.0.1:8080/v1"


@pytest.mark.parametrize(
    "url",
    ["http://example.com/v1", "https://example.com/v1", "http://127.0.0.1:8080/other"],
)
def test_llama_cpp_rejects_nonlocal_or_wrong_route(url: str) -> None:
    with pytest.raises(ModelConfigurationError):
        create_chat_model(
            {
                "KUBEPROOF_AI_PROVIDER": "llama_cpp",
                "KUBEPROOF_AI_MODEL": "selected-gguf",
                "KUBEPROOF_AI_BASE_URL": url,
            }
        )


def test_llama_probe_checks_readiness_and_exact_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def read(url: str, api_key: str) -> object:
        calls.append(url)
        assert api_key == ""
        if url.endswith("/health"):
            return {"status": "ok"}
        return {"data": [{"id": "other"}, {"id": "selected-gguf"}]}

    monkeypatch.setattr("kubeproof.intelligence.model_config._read_local_json", read)
    status = probe_llama_cpp(
        {"KUBEPROOF_AI_PROVIDER": "llama_cpp", "KUBEPROOF_AI_MODEL": "selected-gguf"}
    )
    assert status.model_id == "selected-gguf"
    assert calls == ["http://127.0.0.1:8080/v1/health", "http://127.0.0.1:8080/v1/models"]


def test_llama_probe_does_not_accept_loading_or_a_different_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = {"KUBEPROOF_AI_PROVIDER": "llama_cpp", "KUBEPROOF_AI_MODEL": "selected-gguf"}
    monkeypatch.setattr(
        "kubeproof.intelligence.model_config._read_local_json",
        lambda url, key: {"status": "loading"},
    )
    with pytest.raises(ModelProbeError, match="not ready"):
        probe_llama_cpp(settings)
    monkeypatch.setattr(
        "kubeproof.intelligence.model_config._read_local_json",
        lambda url, key: {"status": "ok"} if url.endswith("/health") else {"data": []},
    )
    with pytest.raises(ModelProbeError, match="model ID"):
        probe_llama_cpp(settings)


def test_plain_http_remote_endpoint_and_missing_remote_key_are_rejected() -> None:
    settings = {
        "KUBEPROOF_AI_PROVIDER": "openai_compatible",
        "KUBEPROOF_AI_MODEL": "remote-model",
        "KUBEPROOF_AI_BASE_URL": "http://example.com/v1",
    }
    with pytest.raises(ModelConfigurationError, match="HTTPS"):
        create_chat_model(settings)
    settings["KUBEPROOF_AI_BASE_URL"] = "https://example.com/v1"
    with pytest.raises(ModelConfigurationError, match="API_KEY"):
        create_chat_model(settings)


def test_api_key_is_selected_from_environment_without_a_pinned_model() -> None:
    model = create_chat_model(
        {
            "KUBEPROOF_AI_PROVIDER": "openai",
            "KUBEPROOF_AI_MODEL": "operator-selected-model",
            "KUBEPROOF_AI_API_KEY": "test-key-not-used-for-a-network-call",
        }
    )
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "operator-selected-model"


def test_missing_provider_and_model_fail_before_network_access() -> None:
    with pytest.raises(ModelConfigurationError, match="PROVIDER"):
        create_chat_model({})


def test_api_provider_is_selected_dynamically(monkeypatch: pytest.MonkeyPatch) -> None:
    configured: dict[str, object] = {}
    fake = FakeListChatModel(responses=["{}"])

    def initialize(model_name: str, **options: object) -> FakeListChatModel:
        configured.update({"model": model_name, **options})
        return fake

    monkeypatch.setattr("kubeproof.intelligence.model_config.init_chat_model", initialize)
    selected = create_chat_model(
        {"KUBEPROOF_AI_PROVIDER": "anthropic", "KUBEPROOF_AI_MODEL": "chosen-by-operator"}
    )
    assert selected is fake
    assert configured["model_provider"] == "anthropic"
    assert configured["model"] == "chosen-by-operator"


def test_supervisor_accepts_only_typed_json_plan() -> None:
    plan = TestPlan(version=1, tasks=(task("cpu-50", 50),))
    model = FakeListChatModel(responses=[plan.model_dump_json(), "not JSON"])
    supervisor = LangChainSupervisor(model)
    assert supervisor.propose(request(), None, (), cpu_capabilities()) == plan
    with pytest.raises(InvalidModelPlan):
        supervisor.propose(request(), None, (), cpu_capabilities())


def test_supervisor_prompt_is_general_and_capabilities_are_run_context() -> None:
    plan = TestPlan(version=1, tasks=(task("cpu-50", 50),))
    model = FakeListChatModel(responses=[])
    with patch.object(
        FakeListChatModel, "invoke", return_value=AIMessage(content=plan.model_dump_json())
    ) as call:
        LangChainSupervisor(model).propose(request(), None, (), cpu_capabilities())
    messages = call.call_args.args[0]
    assert "CPU-test supervisor" not in messages[0].content
    assert "test-planning supervisor" in messages[0].content
    context = json.loads(messages[1].content)
    assert context["available_capabilities"][0]["name"] == "cpu_load"
    assert "parameters_schema" in context["available_capabilities"][0]


def test_natural_language_produces_draft_not_confirmed_policy() -> None:
    draft = RequestDraft(
        environment_id="sandbox-1",
        target_id="cpu-fixture",
        source_fragments={"environment_id": "sandbox-1", "target_id": "cpu-fixture"},
    )
    model = FakeListChatModel(responses=[draft.model_dump_json(), "invalid JSON"])
    extractor = LangChainRequirementsExtractor(model)
    result = extractor.extract("Evaluate cpu-fixture in sandbox-1")
    assert result == draft
    assert result.goal.target_rps is None
    with pytest.raises(InvalidRequirementDraft):
        extractor.extract("Evaluate cpu-fixture in sandbox-1")


def test_draft_rejects_ungrounded_extracted_fields() -> None:
    model = FakeListChatModel(
        responses=[RequestDraft(environment_id="production").model_dump_json()]
    )
    extractor = LangChainRequirementsExtractor(model)
    with pytest.raises(InvalidRequirementDraft, match="source fragments"):
        extractor.extract("Evaluate my local sandbox")
