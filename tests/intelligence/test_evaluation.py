"""Provider responses are proposals, not execution or measurement authority."""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from kubeproof.core.plans import DnsTask, EvaluationPlan
from kubeproof.intelligence.evaluation import EvaluationAI


def test_proposal_exposes_common_catalog_and_makes_one_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = object()
    monkeypatch.setattr("kubeproof.intelligence.evaluation.create_chat_model", lambda: model)
    calls: list[str] = []
    frozen = EvaluationPlan(
        origin="ai", objective="Inspect DNS.", tasks=(DnsTask(id="dns", rationale="inspect"),)
    )

    def invoke(actual: Any, messages: Any, operation: str) -> AIMessage:
        assert actual is model
        calls.append(operation)
        context = json.loads(messages[1].content)
        assert {item["name"] for item in context["capabilities"]} == {
            "http_service",
            "cpu_load",
            "resource_sample",
            "dns_observation",
            "pod_recovery",
        }
        return AIMessage(content=frozen.model_dump_json())

    monkeypatch.setattr("kubeproof.intelligence.evaluation._invoke", invoke)
    ai = EvaluationAI()
    assert ai.propose({"objective": "Inspect DNS."}) == frozen
    assert calls == ["evaluation_plan"]


def test_invalid_ai_output_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kubeproof.intelligence.evaluation.create_chat_model", lambda: object())
    monkeypatch.setattr(
        "kubeproof.intelligence.evaluation._invoke",
        lambda *_: AIMessage(content='{"tasks": [{"capability": "shell"}]}'),
    )
    with pytest.raises(ValueError):
        EvaluationAI().propose({"objective": "Inspect DNS."})
