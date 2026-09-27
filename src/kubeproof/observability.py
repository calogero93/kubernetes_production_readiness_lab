"""Low-cardinality application metrics; never label metrics with user input."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "kubeproof_http_requests_total",
    "HTTP responses by stable route, method and status.",
    ("route", "method", "status"),
)
HTTP_DURATION = Histogram(
    "kubeproof_http_request_duration_seconds",
    "HTTP response latency by stable route and method.",
    ("route", "method"),
)
MODEL_CALLS = Counter(
    "kubeproof_model_calls_total",
    "Model invocations by operation and outcome.",
    ("operation", "outcome"),
)
MODEL_DURATION = Histogram(
    "kubeproof_model_call_duration_seconds",
    "Model invocation latency by operation.",
    ("operation",),
)
LIVE_RUNS = Counter(
    "kubeproof_live_runs_total",
    "Approved local live runs by terminal outcome.",
    ("outcome",),
)


def route_name(method: str, path: str) -> str:
    if path in {"/healthz", "/readyz", "/metrics", "/api/evaluations", "/api/sync"}:
        return path
    if path == "/api/live/config":
        return path
    if path == "/api/live/preflight":
        return path
    if path.startswith("/api/live/runs/"):
        return "/api/live/runs/{id}/approve" if path.endswith("/approve") else "/api/live/runs/{id}"
    if path.startswith("/api/evaluations/"):
        return "/api/evaluations/{id}"
    if path == "/":
        return "/"
    if path.startswith("/assets/"):
        return "/assets/*"
    return "unmatched"


def metrics_response() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
