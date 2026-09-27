from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from urllib.request import urlopen

import pytest

from kubeproof.benchmarks.cpu_http import load
from kubeproof.benchmarks.cpu_http.app import make_server, work_digest


@pytest.fixture
def cpu_fixture_url() -> Iterator[str]:
    server = make_server("127.0.0.1", 0, 1_000)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_fixture_exposes_health_and_verifiable_work(cpu_fixture_url: str) -> None:
    with urlopen(f"{cpu_fixture_url}/healthz", timeout=2) as response:
        assert response.status == 200
        assert response.read() == b"ok\n"
    with urlopen(f"{cpu_fixture_url}/work", timeout=2) as response:
        payload = json.load(response)
    assert payload == {
        "schema_version": "1",
        "iterations": 1_000,
        "digest": work_digest(1_000),
    }


def test_fixed_rate_run_counts_and_reports_success(cpu_fixture_url: str) -> None:
    summary = load.run_load(
        f"{cpu_fixture_url}/work", target_rps=20, requests=3, work_iterations=1_000
    )
    assert summary.planned_requests == 3
    assert summary.sent_requests == 3
    assert summary.dropped_by_client == 0
    assert summary.succeeded == 3
    assert summary.failed == 0
    assert summary.latency_ms_p95 is not None
    assert summary.max_schedule_lag_ms >= 0
    assert 0 < summary.success_rps <= 20


def test_client_saturation_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_request(*_args: object) -> load.RequestResult:
        time.sleep(0.15)
        return load.RequestResult(150)

    monkeypatch.setattr(load, "_request", slow_request)
    summary = load.run_load(
        "http://127.0.0.1:8080/work",
        target_rps=200,
        requests=5,
        work_iterations=1_000,
        max_in_flight=1,
    )
    assert summary.sent_requests == 1
    assert summary.dropped_by_client == 4
    assert summary.succeeded == 1
    assert summary.success_rps < summary.target_rps


def test_failure_has_no_success_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(load, "_request", lambda *_args: load.RequestResult(1.0, "network_error"))
    summary = load.run_load(
        "http://127.0.0.1:8080/work",
        target_rps=10,
        requests=1,
        work_iterations=1_000,
    )
    assert summary.succeeded == 0
    assert summary.failed == 1
    assert summary.failure_kinds == {"network_error": 1}
    assert summary.latency_ms_p95 is None


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8080/work",
        "http://example.com:8080/work",
        "http://127.0.0.1:8080/other",
        "http://127.0.0.1/work",
        "http://127.0.0.1:8080/work?iterations=9999999",
    ],
)
def test_load_driver_rejects_non_fixture_targets(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        load.validate_target(url)


def test_cpu_work_is_bounded() -> None:
    with pytest.raises(ValueError, match="iterations"):
        work_digest(0)
    with pytest.raises(ValueError, match="iterations"):
        work_digest(2_000_001)
