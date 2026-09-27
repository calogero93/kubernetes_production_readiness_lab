from __future__ import annotations

import json

import pytest

from kubeproof.execution.service_probe import ServiceProbeError, job_manifest, validate_result
from kubeproof.execution.service_probe_runner import probe


def test_job_targets_only_confirmed_service_and_has_bounded_permissions() -> None:
    job = job_manifest(
        name="kubeproof-probe-123",
        namespace="sandbox",
        service="api",
        port=8080,
        path="/healthz",
        requests=10,
        image="kubeproof:local",
    )
    spec = job["spec"]["template"]["spec"]
    container = spec["containers"][0]
    assert spec["automountServiceAccountToken"] is False
    assert spec["restartPolicy"] == "Never"
    assert container["args"] == ["api", "sandbox", "8080", "/healthz", "10"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert job["spec"]["backoffLimit"] == 0


@pytest.mark.parametrize("path", ["//evil", "/healthz?x=1", "http://evil"])
def test_job_rejects_unreviewed_paths(path: str) -> None:
    with pytest.raises(ValueError):
        job_manifest(
            name="probe",
            namespace="sandbox",
            service="api",
            port=80,
            path=path,
            requests=10,
            image="kubeproof:local",
        )


def test_runner_summarizes_success_and_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, status: int):
            self.status = status

        def read(self, limit: int) -> bytes:
            assert limit == 4096
            return b"ok"

    class Connection:
        calls = 0

        def __init__(self, host: str, port: int, timeout: int):
            assert (host, port, timeout) == ("api.sandbox.svc", 80, 3)

        def request(self, method: str, path: str) -> None:
            assert (method, path) == ("GET", "/healthz")

        def getresponse(self) -> Response:
            Connection.calls += 1
            return Response(200 if Connection.calls == 1 else 503)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "kubeproof.execution.service_probe_runner.http.client.HTTPConnection", Connection
    )
    result = probe("api.sandbox.svc", 80, "/healthz", 2)
    assert result["outcomes"] == {"200": 1, "503": 1}
    assert result["passed"] is False
    assert json.loads(json.dumps(result))["requests"] == 2


def test_result_validation_recomputes_pass_from_observed_statuses() -> None:
    result = {
        "schema_version": "1",
        "target": "http://api.sandbox.svc:80/healthz",
        "requests": 2,
        "outcomes": {"200": 1, "503": 1},
        "p95_ms": 12.5,
        "passed": True,
    }
    with pytest.raises(ServiceProbeError):
        validate_result(
            result,
            service="api",
            namespace="sandbox",
            port=80,
            path="/healthz",
            requests=2,
        )
    result["passed"] = False
    assert (
        validate_result(
            result,
            service="api",
            namespace="sandbox",
            port=80,
            path="/healthz",
            requests=2,
        )
        == result
    )
