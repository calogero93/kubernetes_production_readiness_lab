"""Local JSON API and static delivery of the separately built React frontend."""

from __future__ import annotations

import json
import mimetypes
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from kubeproof.evidence.history import HistoryError, HistoryService

DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def make_handler(
    service: HistoryService, frontend_dir: Path | None = None
) -> type[BaseHTTPRequestHandler]:
    frontend_root = (frontend_dir or DEFAULT_FRONTEND_DIST).resolve()

    class HistoryHandler(BaseHTTPRequestHandler):
        def _respond(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
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
            path = urlparse(self.path).path
            try:
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
            if urlparse(self.path).path != "/api/sync":
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
    server = ThreadingHTTPServer((host, port), make_handler(service, frontend_dir))
    try:
        server.serve_forever()
    finally:
        server.server_close()
