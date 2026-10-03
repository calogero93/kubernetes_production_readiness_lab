from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from kubeproof.execution.service_probe import (
    ServiceProbeCancelled,
    ServiceProbeError,
    job_manifest,
    run_service_probe,
    validate_result,
)
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
    assert result["all_responses_200"] is False
    assert len(result["latencies_ms"]) == 2
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


def test_schema_two_p95_is_recomputed_from_individual_request_timings() -> None:
    result = {
        "schema_version": "2",
        "target": "http://api.sandbox.svc:80/healthz",
        "requests": 2,
        "outcomes": {"200": 2},
        "p95_ms": 12.5,
        "latencies_ms": [1, 12.5],
        "all_responses_200": True,
    }
    assert (
        validate_result(
            result, service="api", namespace="sandbox", port=80, path="/healthz", requests=2
        )
        == result
    )
    result["p95_ms"] = 1
    with pytest.raises(ServiceProbeError, match="inconsistent request timings"):
        validate_result(
            result, service="api", namespace="sandbox", port=80, path="/healthz", requests=2
        )


@pytest.mark.parametrize("mode", ["success", "invalid", "cancelled", "failed"])
def test_probe_uses_explicit_cluster_client_and_deletes_its_job(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    api = object()
    created: list[str] = []
    deleted: list[str] = []
    result = {
        "schema_version": "2",
        "target": "http://api.sandbox.svc:80/healthz",
        "requests": 2,
        "outcomes": {"200": 2},
        "p95_ms": 12.5,
        "latencies_ms": [1, 12.5],
        "all_responses_200": True,
    }
    if mode == "invalid":
        result["p95_ms"] = 1

    class Batch:
        def __init__(self, received: object):
            assert received is api

        def create_namespaced_job(
            self, namespace: str, manifest: dict[str, Any], **kwargs: Any
        ) -> None:
            created.append(manifest["metadata"]["name"])

        def read_namespaced_job_status(self, *_args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                status=SimpleNamespace(succeeded=mode != "failed", failed=mode == "failed")
            )

        def delete_namespaced_job(self, name: str, *_args: Any, **kwargs: Any) -> None:
            deleted.append(name)

    class Core:
        def __init__(self, received: object):
            assert received is api

        def read_namespaced_service(self, *_args: Any, **kwargs: Any) -> None:
            pass

        def list_namespaced_pod(self, *_args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                items=[SimpleNamespace(metadata=SimpleNamespace(name="probe-pod"))]
            )

        def read_namespaced_pod_log(self, *_args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(data=json.dumps(result).encode(), close=lambda: None)

    monkeypatch.setattr("kubeproof.execution.service_probe.client.BatchV1Api", Batch)
    monkeypatch.setattr("kubeproof.execution.service_probe.client.CoreV1Api", Core)
    arguments = dict(
        service="api",
        namespace="sandbox",
        port=80,
        path="/healthz",
        requests=2,
        image="probe:local",
        api_client=api,
        cancel_if=lambda: bool(created) and mode == "cancelled",
    )
    if mode == "success":
        assert run_service_probe(**arguments) == result
    else:
        error = ServiceProbeCancelled if mode == "cancelled" else ServiceProbeError
        with pytest.raises(error):
            run_service_probe(**arguments)
    assert len(created) == 1
    assert deleted == created
