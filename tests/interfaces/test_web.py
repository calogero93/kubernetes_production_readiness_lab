from __future__ import annotations

import json
from http import HTTPStatus
from io import BytesIO
from pathlib import Path

from kubeproof.evidence.history import HistoryService
from kubeproof.interfaces.web import make_handler


def _get(service: HistoryService, frontend: Path, path: str) -> tuple[int, bytes, bytes]:
    # Exercise the real handler without opening a socket in restricted CI/sandboxes.
    handler = object.__new__(make_handler(service, frontend))
    handler.wfile = BytesIO()
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.client_address = ("127.0.0.1", 0)
    handler.log_message = lambda *_: None  # type: ignore[method-assign]
    handler.do_GET()
    headers, body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    status = int(headers.split(b"\r\n", 1)[0].split()[1])
    return status, headers, body


def test_history_api_and_built_frontend_are_served_separately(tmp_path: Path) -> None:
    frontend = tmp_path / "dist"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text('<div id="root"></div>')
    (frontend / "assets" / "app.js").write_text("console.log('frontend')")
    service = HistoryService.local(tmp_path / "data")

    status, _, body = _get(service, frontend, "/")
    assert status == HTTPStatus.OK
    assert b'id="root"' in body

    status, headers, body = _get(service, frontend, "/assets/app.js")
    assert status == HTTPStatus.OK
    assert b"Content-Type: text/javascript; charset=utf-8" in headers
    assert b"frontend" in body

    status, _, body = _get(service, frontend, "/api/evaluations")
    assert status == HTTPStatus.OK
    assert json.loads(body) == []

    status, _, _ = _get(service, frontend, "/assets/%2e%2e/%2e%2e/secret")
    assert status == HTTPStatus.NOT_FOUND


def test_unbuilt_frontend_has_actionable_error(tmp_path: Path) -> None:
    service = HistoryService.local(tmp_path / "data")
    status, _, body = _get(service, tmp_path / "dist", "/")
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert "npm run build" in json.loads(body)["error"]


def test_frontend_index_symlink_cannot_escape_build_directory(tmp_path: Path) -> None:
    frontend = tmp_path / "dist"
    frontend.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("private")
    (frontend / "index.html").symlink_to(secret)
    service = HistoryService.local(tmp_path / "data")
    status, _, body = _get(service, frontend, "/")
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert b"private" not in body
