"""LangChain adapters that propose plans and extract reviewable drafts."""

from __future__ import annotations

import json
import os
import time
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError

from kubeproof.intelligence.capabilities import CapabilityCatalog
from kubeproof.intelligence.models import ConfirmedRequest, RequestDraft, TestPlan, TrialRecord
from kubeproof.observability import MODEL_CALLS, MODEL_DURATION


class InvalidModelPlan(ValueError):
    """The model did not return a valid typed plan."""


class InvalidRequirementDraft(ValueError):
    """The model did not return a typed requirement extraction."""


class Supervisor(Protocol):
    def propose(
        self,
        request: ConfirmedRequest,
        previous: TestPlan | None,
        records: tuple[TrialRecord, ...],
        catalog: CapabilityCatalog,
    ) -> TestPlan: ...


def _invoke(
    model: BaseChatModel, messages: list[SystemMessage | HumanMessage], operation: str
) -> BaseMessage:
    """Observe one model call; Langfuse is explicit opt-in because it receives prompts."""
    keys = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
    configured = [bool(os.environ.get(key)) for key in keys]
    if any(configured) and not all(configured):
        raise ValueError("Langfuse requires public key, secret key, and base URL")
    config: RunnableConfig | None = None
    if all(configured):
        try:
            from langfuse.langchain import CallbackHandler
        except ImportError as exc:
            raise ValueError("install the tracing extra to enable Langfuse") from exc
        config = {"callbacks": [CallbackHandler()]}
    started = time.perf_counter()
    try:
        response = model.invoke(messages, config=config)
    except Exception:
        MODEL_CALLS.labels(operation, "error").inc()
        raise
    else:
        MODEL_CALLS.labels(operation, "response").inc()
        return response
    finally:
        MODEL_DURATION.labels(operation).observe(time.perf_counter() - started)


class LangChainSupervisor:
    """Only proposes plans; it has no executable tools or authority to admit them."""

    def __init__(self, model: BaseChatModel):
        self._model = model

    def propose(
        self,
        request: ConfirmedRequest,
        previous: TestPlan | None,
        records: tuple[TrialRecord, ...],
        catalog: CapabilityCatalog,
    ) -> TestPlan:
        context = {
            "confirmed_request": request.model_dump(mode="json"),
            "previous_plan": previous.model_dump(mode="json") if previous else None,
            "trial_records": [record.model_dump(mode="json") for record in records],
            "next_plan_version": 1 if previous is None else previous.version + 1,
            "plan_schema": TestPlan.model_json_schema(),
            "available_capabilities": catalog.planning_context(),
        }
        message = _invoke(
            self._model,
            [
                SystemMessage(
                    content=(
                        "You are KubeProof's test-planning supervisor. Propose one versioned plan "
                        "for the confirmed objective using only available_capabilities. Return one "
                        "JSON object matching plan_schema, with no markdown or extra text. "
                        "Preserve all existing tasks unchanged in a revision. "
                        "Respect the confirmed budget "
                        "and task dependencies. Do not claim a test ran; only propose the plan. "
                        "Execution, authorization, scheduling and assessment are deterministic."
                    )
                ),
                HumanMessage(content=json.dumps(context, sort_keys=True)),
            ],
            "plan",
        )
        if not isinstance(message.content, str):
            raise InvalidModelPlan("model response was not plain JSON text")
        try:
            return TestPlan.model_validate_json(message.content)
        except ValidationError as exc:
            raise InvalidModelPlan("model response did not match the plan schema") from exc


class LangChainRequirementsExtractor:
    """Extracts a reviewable draft; only a separate user action can confirm it."""

    def __init__(self, model: BaseChatModel):
        self._model = model

    def extract(self, description: str) -> RequestDraft:
        if not description.strip():
            raise ValueError("evaluation description cannot be empty")
        message = _invoke(
            self._model,
            [
                SystemMessage(
                    content=(
                        "Extract only requirements explicitly present in the user's text. "
                        "Return one JSON object matching the supplied schema, with no markdown. "
                        "Use null for absent fields. Put unclear statements in "
                        "ambiguous_statements and relevant unsupported text in "
                        "unmapped_statements. For every non-null field, include an exact quote "
                        "from the user's text in source_fragments, keyed by dotted field path "
                        "such as goal.target_rps. Never invent budgets or permissions."
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {
                            "description": description,
                            "draft_schema": RequestDraft.model_json_schema(),
                        },
                        sort_keys=True,
                    )
                ),
            ],
            "draft",
        )
        if not isinstance(message.content, str):
            raise InvalidRequirementDraft("model response was not plain JSON text")
        try:
            draft = RequestDraft.model_validate_json(message.content)
        except ValidationError as exc:
            raise InvalidRequirementDraft("model response did not match the draft schema") from exc
        values = draft.model_dump(exclude_none=True)
        present_fields = {
            key for key in ("environment_id", "target_id", "work_iterations") if key in values
        }
        for group in ("goal", "budget"):
            present_fields.update(f"{group}.{key}" for key in values[group])
        if set(draft.source_fragments) != present_fields:
            raise InvalidRequirementDraft("source fragments must match extracted fields exactly")
        for key in present_fields:
            fragment = draft.source_fragments.get(key, "")
            if not fragment or fragment not in description:
                raise InvalidRequirementDraft(f"missing exact source text for {key}")
        if any(fragment not in description for fragment in draft.source_fragments.values()):
            raise InvalidRequirementDraft("a source fragment was not present in the input text")
        return draft
