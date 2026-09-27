"""Provider-neutral model selection and safe local-server preflight."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


class ModelConfigurationError(ValueError):
    """The selected model endpoint or provider is not safely configured."""


class ModelProbeError(ValueError):
    """The configured local model server is unavailable or incompatible."""


@dataclass(frozen=True)
class LocalModelStatus:
    """The configured model ID was found on the selected server."""

    base_url: str
    model_id: str


def _configured_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ModelConfigurationError("model base URL must be an http(s) URL without credentials")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ModelConfigurationError("non-loopback model endpoints require HTTPS")
    return value.rstrip("/")


def _llama_cpp_endpoint(settings: Mapping[str, str]) -> str:
    base_url = settings.get("KUBEPROOF_AI_BASE_URL", "").strip() or "http://127.0.0.1:8080/v1"
    endpoint = _configured_base_url(base_url)
    parsed = urlsplit(endpoint)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ModelConfigurationError("llama_cpp requires a loopback endpoint")
    if parsed.path != "/v1":
        raise ModelConfigurationError("llama_cpp base URL must end in /v1")
    return endpoint


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, request: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def _read_local_json(url: str, api_key: str) -> object:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request = Request(url, headers=headers)
    try:
        with build_opener(_NoRedirects).open(request, timeout=3) as response:
            body = response.read(1_000_001)
    except HTTPError as exc:
        if exc.code == 503:
            raise ModelProbeError("model server is still loading") from exc
        raise ModelProbeError(f"model server returned HTTP {exc.code}") from exc
    except (OSError, URLError) as exc:
        raise ModelProbeError("cannot connect to the model server") from exc
    if len(body) > 1_000_000:
        raise ModelProbeError("model server response is too large")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelProbeError("model server returned invalid JSON") from exc


def probe_llama_cpp(env: Mapping[str, str] | None = None) -> LocalModelStatus:
    """Check readiness and selected model ID; do not invoke inference or tools."""
    settings = os.environ if env is None else env
    if settings.get("KUBEPROOF_AI_PROVIDER", "").strip() != "llama_cpp":
        raise ModelConfigurationError("model probe requires KUBEPROOF_AI_PROVIDER=llama_cpp")
    model_id = settings.get("KUBEPROOF_AI_MODEL", "").strip()
    if not model_id:
        raise ModelConfigurationError("KUBEPROOF_AI_MODEL is required")
    endpoint = _llama_cpp_endpoint(settings)
    api_key = settings.get("KUBEPROOF_AI_API_KEY", "").strip()
    health = _read_local_json(f"{endpoint}/health", api_key)
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise ModelProbeError("local model server is not ready")
    models = _read_local_json(f"{endpoint}/models", api_key)
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        raise ModelProbeError("local model server returned an invalid model list")
    if not any(isinstance(item, dict) and item.get("id") == model_id for item in models["data"]):
        raise ModelProbeError("configured model ID is not exposed by the local server")
    return LocalModelStatus(base_url=endpoint, model_id=model_id)


def probe_compatible_model(env: Mapping[str, str] | None = None) -> LocalModelStatus:
    """Check a selected OpenAI-compatible model, including vLLM, without inference."""
    settings = os.environ if env is None else env
    if settings.get("KUBEPROOF_AI_PROVIDER", "").strip() != "openai_compatible":
        raise ModelConfigurationError("compatible probe requires openai_compatible provider")
    model = settings.get("KUBEPROOF_AI_MODEL", "").strip()
    base_url = settings.get("KUBEPROOF_AI_BASE_URL", "").strip()
    if not model or not base_url:
        raise ModelConfigurationError("model ID and base URL are required")
    endpoint = _configured_base_url(base_url)
    key = settings.get("KUBEPROOF_AI_API_KEY", "").strip()
    if urlsplit(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"} and not key:
        raise ModelConfigurationError("remote compatible endpoints require KUBEPROOF_AI_API_KEY")
    models = _read_local_json(f"{endpoint}/models", key)
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        raise ModelProbeError("compatible model server returned an invalid model list")
    if not any(isinstance(item, dict) and item.get("id") == model for item in models["data"]):
        raise ModelProbeError("configured model ID is not exposed by the server")
    return LocalModelStatus(base_url=endpoint, model_id=model)


def create_chat_model(env: Mapping[str, str] | None = None) -> BaseChatModel:
    """Select a model by environment, without hard-coding its provider or name.

    llama.cpp uses `KUBEPROOF_AI_PROVIDER=llama_cpp`, with a loopback /v1 URL.
    Other compatible servers use `openai_compatible` and an explicit base URL.
    Other API providers use LangChain's provider integration package and standard key env.
    """
    settings = os.environ if env is None else env
    provider = settings.get("KUBEPROOF_AI_PROVIDER", "").strip()
    model_name = settings.get("KUBEPROOF_AI_MODEL", "").strip()
    if not provider or not model_name:
        raise ModelConfigurationError("KUBEPROOF_AI_PROVIDER and KUBEPROOF_AI_MODEL are required")

    base_url = settings.get("KUBEPROOF_AI_BASE_URL", "").strip()
    if provider in {"openai_compatible", "llama_cpp"}:
        if provider == "openai_compatible" and not base_url:
            raise ModelConfigurationError("openai_compatible requires KUBEPROOF_AI_BASE_URL")
        endpoint = (
            _llama_cpp_endpoint(settings)
            if provider == "llama_cpp"
            else _configured_base_url(base_url)
        )
        key = settings.get("KUBEPROOF_AI_API_KEY", "").strip()
        if urlsplit(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"} and not key:
            raise ModelConfigurationError(
                "remote compatible endpoints require KUBEPROOF_AI_API_KEY"
            )
        return ChatOpenAI(
            model=model_name,
            base_url=endpoint,
            api_key=SecretStr(key or "local-no-key"),
            timeout=30,
            max_retries=0,
        )

    if base_url:
        raise ModelConfigurationError("base URL is supported only for compatible model servers")
    if provider == "openai":
        key = (
            settings.get("KUBEPROOF_AI_API_KEY", "").strip()
            or settings.get("OPENAI_API_KEY", "").strip()
        )
        if not key:
            raise ModelConfigurationError("openai requires an API key environment variable")
        return ChatOpenAI(
            model=model_name,
            api_key=SecretStr(key),
            timeout=30,
            max_retries=0,
        )
    if provider != "openai" and "KUBEPROOF_AI_API_KEY" in settings:
        raise ModelConfigurationError(
            "use the selected provider's standard credential environment variable"
        )
    try:
        return init_chat_model(
            model_name,
            model_provider=provider,
            timeout=30,
            max_retries=0,
        )
    except (ImportError, ValueError) as exc:
        raise ModelConfigurationError(f"cannot initialize provider {provider!r}: {exc}") from exc
