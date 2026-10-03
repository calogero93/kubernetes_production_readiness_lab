"""Local JSON API and static delivery of the separately built React frontend."""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse, urlsplit

from kubeproof.evidence.history import HistoryError, HistoryService
from kubeproof.observability import HTTP_DURATION, HTTP_REQUESTS, metrics_response, route_name

if TYPE_CHECKING:
    from kubeproof.interfaces.chart_jobs import ChartJobManager
    from kubeproof.interfaces.live_jobs import LiveJobManager

DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def make_handler(
    service: HistoryService,
    frontend_dir: Path | None = None,
    live: LiveJobManager | None = None,
    chart: ChartJobManager | None = None,
) -> type[BaseHTTPRequestHandler]:
    frontend_root = (frontend_dir or DEFAULT_FRONTEND_DIST).resolve()

    class HistoryHandler(BaseHTTPRequestHandler):
        def _has_local_host(self) -> bool:
            host = self.headers.get("Host", "")
            try:
                parsed = urlsplit(f"//{host}")
            except ValueError:
                return False
            return (
                parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.username is None
                and parsed.password is None
            )

        def _read_json(self, csrf_token: str) -> dict[str, Any]:
            if not self._has_local_host():
                raise ValueError("local API requires a loopback Host header")
            if self.headers.get("X-Kubeproof-CSRF") != csrf_token:
                raise ValueError("missing or invalid local approval token")
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise ValueError("Content-Length is required") from exc
            if not 0 < length <= 15 * 1024 * 1024:
                raise ValueError("request body exceeds the 15 MiB limit")
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("request body is not valid JSON") from exc
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            return body

        def _respond(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            path = urlparse(self.path).path
            method = getattr(self, "command", "GET")
            route = route_name(method, path)
            HTTP_REQUESTS.labels(route, method, str(status.value)).inc()
            HTTP_DURATION.labels(route, method).observe(
                max(0.0, time.perf_counter() - getattr(self, "_started_at", time.perf_counter()))
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._respond(status, _json_bytes({"error": message}), "application/json")

        def _static(self, path: str) -> None:
            if path == "/":
                candidate = (frontend_root / "index.html").resolve()
                if not candidate.is_relative_to(frontend_root) or not candidate.is_file():
                    self._error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "frontend is not built; run npm ci and npm run build in frontend/",
                    )
                    return
            else:
                candidate = (frontend_root / unquote(path).lstrip("/")).resolve()
                if not candidate.is_relative_to(frontend_root) or not candidate.is_file():
                    self._error(HTTPStatus.NOT_FOUND, "not found")
                    return
            try:
                body = candidate.read_bytes()
            except OSError:
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            if content_type.startswith("text/"):
                content_type += "; charset=utf-8"
            self._respond(HTTPStatus.OK, body, content_type)

        def do_GET(self) -> None:
            self._started_at = time.perf_counter()
            path = urlparse(self.path).path
            try:
                if (
                    (live is not None or chart is not None)
                    and path.startswith("/api/")
                    and not self._has_local_host()
                ):
                    self._error(HTTPStatus.BAD_REQUEST, "local API requires a loopback Host header")
                    return
                if path == "/metrics":
                    body, content_type = metrics_response()
                    self._respond(HTTPStatus.OK, body, content_type)
                    return
                if path in {"/healthz", "/readyz"}:
                    self._respond(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
                    return
                if path == "/api/live/config":
                    self._respond(
                        HTTPStatus.OK,
                        _json_bytes(
                            {
                                "available": live is not None,
                                "csrf_token": live.csrf_token if live is not None else None,
                            }
                        ),
                        "application/json",
                    )
                    return
                if path == "/api/chart/config":
                    self._respond(
                        HTTPStatus.OK,
                        _json_bytes(
                            {
                                "available": chart is not None,
                                "csrf_token": chart.csrf_token if chart is not None else None,
                            }
                        ),
                        "application/json",
                    )
                    return
                if path.startswith("/api/chart/runs/"):
                    if chart is None:
                        self._error(HTTPStatus.SERVICE_UNAVAILABLE, "chart runs are unavailable")
                        return
                    from kubeproof.interfaces.chart_jobs import ChartJobError

                    try:
                        chart_payload = chart.get(path.removeprefix("/api/chart/runs/"))
                    except ChartJobError as exc:
                        self._error(HTTPStatus.NOT_FOUND, str(exc))
                        return
                    self._respond(HTTPStatus.OK, _json_bytes(chart_payload), "application/json")
                    return
                if path.startswith("/api/live/runs/"):
                    if live is None:
                        self._error(
                            HTTPStatus.SERVICE_UNAVAILABLE, "live CPU evaluation is unavailable"
                        )
                        return
                    job_id = path.removeprefix("/api/live/runs/")
                    from kubeproof.interfaces.live_jobs import LiveJobError

                    try:
                        live_payload = live.get(job_id)
                    except LiveJobError as exc:
                        self._error(HTTPStatus.NOT_FOUND, str(exc))
                        return
                    self._respond(HTTPStatus.OK, _json_bytes(live_payload), "application/json")
                    return
                if path == "/api/evaluations":
                    payload = [asdict(item) for item in service.list_evaluations()]
                    self._respond(HTTPStatus.OK, _json_bytes(payload), "application/json")
                    return
                prefix = "/api/evaluations/"
                if path.startswith(prefix):
                    evaluation_id = unquote(path[len(prefix) :])
                    evaluation = service.get_evaluation(evaluation_id)
                    if evaluation is None:
                        self._error(HTTPStatus.NOT_FOUND, "evaluation not found")
                        return
                    self._respond(
                        HTTPStatus.OK,
                        _json_bytes(evaluation.model_dump(mode="json")),
                        "application/json",
                    )
                    return
                if path == "/" or path.startswith("/assets/"):
                    self._static(path)
                    return
                self._error(HTTPStatus.NOT_FOUND, "not found")
            except HistoryError as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))

        def do_POST(self) -> None:
            self._started_at = time.perf_counter()
            path = urlparse(self.path).path
            if path == "/api/live/preflight" or (
                path.startswith("/api/live/runs/") and path.endswith("/approve")
            ):
                if live is None:
                    self._error(
                        HTTPStatus.SERVICE_UNAVAILABLE, "live CPU evaluation is unavailable"
                    )
                    return
                from kubeproof.evidence.bundle import BundleError
                from kubeproof.execution.helm import HelmRenderError
                from kubeproof.interfaces.live_jobs import LiveJobError

                try:
                    body = self._read_json(live.csrf_token)
                    if path == "/api/live/preflight":
                        payload = live.prepare(body)
                    else:
                        job_id = (
                            path.removeprefix("/api/live/runs/")
                            .removesuffix("/approve")
                            .rstrip("/")
                        )
                        digest = body.get("chart_sha256")
                        if not isinstance(digest, str):
                            raise LiveJobError("chart_sha256 is required for exact approval")
                        payload = live.approve(job_id, digest)
                except (ValueError, HelmRenderError, BundleError, OSError) as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self._respond(HTTPStatus.OK, _json_bytes(payload), "application/json")
                return
            if path == "/api/chart/preflight" or (
                path.startswith("/api/chart/runs/") and path.endswith("/approve")
            ):
                if chart is None:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "chart runs are unavailable")
                    return
                from kubeproof.evidence.bundle import BundleError
                from kubeproof.execution.helm import HelmRenderError

                try:
                    body = self._read_json(chart.csrf_token)
                    if path == "/api/chart/preflight":
                        payload = chart.prepare(body)
                    else:
                        job_id = (
                            path.removeprefix("/api/chart/runs/")
                            .removesuffix("/approve")
                            .rstrip("/")
                        )
                        chart_sha256 = body.get("chart_sha256")
                        rendered_sha256 = body.get("rendered_manifest_sha256")
                        if not isinstance(chart_sha256, str) or not isinstance(
                            rendered_sha256, str
                        ):
                            raise ValueError("chart and rendered manifest SHA-256 are required")
                        approval_fields = (
                            body.get("approval_scope_sha256"),
                            body.get("operator_label"),
                            body.get("approval_reason"),
                        )
                        if any(
                            value is not None and not isinstance(value, str)
                            for value in approval_fields
                        ):
                            raise ValueError("approval fields must be strings")
                        payload = chart.approve(
                            job_id,
                            chart_sha256=chart_sha256,
                            rendered_manifest_sha256=rendered_sha256,
                            approval_scope_sha256=approval_fields[0],
                            operator_label=approval_fields[1],
                            approval_reason=approval_fields[2],
                            plan_sha256=body.get("plan_sha256"),
                        )
                except (ValueError, HelmRenderError, BundleError, OSError) as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self._respond(HTTPStatus.OK, _json_bytes(payload), "application/json")
                return
            if path != "/api/sync":
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            try:
                count = service.sync()
            except HistoryError as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))
                return
            self._respond(HTTPStatus.OK, _json_bytes({"indexed": count}), "application/json")

    return HistoryHandler


def serve_history(
    service: HistoryService, host: str, port: int, frontend_dir: Path | None = None
) -> None:
    live = None
    chart = None
    if host in {"127.0.0.1", "localhost", "::1"}:
        from kubeproof.interfaces.chart_jobs import ChartJobManager

        run_lock = threading.Lock()
        chart = ChartJobManager(service, run_lock)
        try:
            from kubeproof.interfaces.live_jobs import LiveJobManager
        except ImportError:
            pass
        else:
            live = LiveJobManager(service, run_lock)
    server = ThreadingHTTPServer((host, port), make_handler(service, frontend_dir, live, chart))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if live is not None:
            live.close()
        if chart is not None:
            chart.close()
