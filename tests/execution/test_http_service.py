from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kubeproof.core.domain import Assessment, ExecutionStatus, HttpProbeOptions
from kubeproof.core.manifests import parse_manifests
from kubeproof.execution.helm import HelmRenderRequest
from kubeproof.execution.http_service import HttpServiceExperiment
from kubeproof.execution.runtime_monitor import UnsafeWorkloadError
from kubeproof.execution.service_probe import ServiceProbeCancelled, ServiceProbeError


@pytest.mark.parametrize(
    "invalid",
    [
        {"service": "external.example"},
        {"path": "//external"},
        {"path": "/?secret=1"},
        {"requests": 51},
        {"requests": True},
        {"max_failed_requests": 10},
        {"max_p95_ms": float("nan")},
        {"max_p95_ms": True},
        {"max_p95_ms": -1},
    ],
)
def test_http_parameters_are_bounded_before_execution(invalid: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        HttpProbeOptions(**{"service": "api", **invalid})


def experiment(http_manifest: bytes, **options: Any) -> HttpServiceExperiment:
    probe = HttpServiceExperiment(HttpProbeOptions(service="api", port=8080, **options))
    probe.validate_resources(parse_manifests(http_manifest), HelmRenderRequest(chart="chart.tgz"))
    probe.image_id = "sha256:probe-image"
    return probe


def cluster_for(http_manifest: bytes) -> Any:
    service = parse_manifests(http_manifest)[0]
    return SimpleNamespace(
        core=SimpleNamespace(read_namespaced_service=lambda *_args, **_kwargs: service),
        api_client=SimpleNamespace(sanitize_for_serialization=lambda item: item),
    )


@pytest.mark.parametrize(
    ("outcomes", "options", "expected"),
    [
        ({"200": 10}, {}, Assessment.PASS),
        ({"204": 10}, {"expected_status": 204}, Assessment.PASS),
        ({"200": 9, "503": 1}, {}, Assessment.FAIL),
        ({"200": 9, "503": 1}, {"max_failed_requests": 1}, Assessment.PASS),
        ({"network_error": 10}, {}, Assessment.FAIL),
        ({"200": 10}, {"max_p95_ms": 5}, Assessment.FAIL),
    ],
)
def test_http_probe_assesses_confirmed_requirements_and_preserves_timings(
    http_manifest: bytes,
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, int],
    options: dict[str, Any],
    expected: Assessment,
) -> None:
    probe = experiment(http_manifest, **options)
    cluster = cluster_for(http_manifest)

    def run(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["api_client"] is cluster.api_client
        assert kwargs["service"] == "api"
        assert kwargs["namespace"] == "kubeproof-product"
        assert kwargs["port"] == 8080
        return {
            "schema_version": "2",
            "target": "http://api.kubeproof-product.svc:8080/healthz",
            "requests": 10,
            "outcomes": outcomes,
            "latencies_ms": list(range(1, 11)),
            "p95_ms": 10,
            "all_responses_200": outcomes == {"200": 10},
        }

    monkeypatch.setattr("kubeproof.execution.http_service.run_service_probe", run)
    result = probe.run(cluster, Path("config"), "kubeproof-product", lambda: False)
    assert result.checks[0].execution_status is ExecutionStatus.COMPLETED
    assert result.checks[0].assessment is expected
    assert result.observations[0].data["measurement"]["latencies_ms"] == list(range(1, 11))
    assert result.observations[0].resource.kind == "Service"
    assert result.observations[0].data["probe_image_id"] == "sha256:probe-image"
    if expected is Assessment.FAIL:
        assert result.findings[0].observation_ids == (result.observations[0].id,)
    else:
        assert not result.findings
    assert "artifacts/http/service-probe.json" in result.artifacts


def test_probe_job_failure_is_not_a_product_failure(
    http_manifest: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = experiment(http_manifest)

    def fail(**kwargs: Any) -> Any:
        raise ServiceProbeError("probe image could not start")

    monkeypatch.setattr("kubeproof.execution.http_service.run_service_probe", fail)
    result = probe.run(cluster_for(http_manifest), Path("config"), "product", lambda: False)
    assert result.checks[0].execution_status is ExecutionStatus.INFRASTRUCTURE_FAILURE
    assert result.checks[0].assessment is Assessment.NOT_TESTED
    assert not result.observations
    assert not result.findings


def test_safety_cancellation_stops_the_http_experiment(
    http_manifest: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = experiment(http_manifest)

    def cancel(**kwargs: Any) -> Any:
        raise ServiceProbeCancelled("dynamic unsafe Pod")

    monkeypatch.setattr("kubeproof.execution.http_service.run_service_probe", cancel)
    with pytest.raises(UnsafeWorkloadError, match="dynamic unsafe Pod"):
        probe.run(cluster_for(http_manifest), Path("config"), "product", lambda: False)


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "ExternalName", "externalName": "external.example"},
        {"clusterIP": "None"},
        {"selector": {}},
        {"ports": [{"port": 8080, "protocol": "UDP"}]},
        {"externalIPs": ["1.2.3.4"]},
        {"ports": None},
        {"ports": ["invalid-port"]},
    ],
)
def test_preflight_rejects_unsupported_service_targets(
    http_manifest: bytes,
    changes: dict[str, Any],
) -> None:
    resources = parse_manifests(http_manifest)
    resources[0]["spec"].update(changes)
    probe = HttpServiceExperiment(HttpProbeOptions(service="api", port=8080))
    with pytest.raises(ValueError):
        probe.validate_resources(resources, HelmRenderRequest(chart="chart.tgz"))


def test_probe_rejects_a_live_service_selector_changed_after_review(http_manifest: bytes) -> None:
    probe = experiment(http_manifest)
    cluster = cluster_for(http_manifest)
    service = cluster.core.read_namespaced_service("api", "product")
    service["spec"]["selector"] = {"app": "different"}
    result = probe.run(cluster, Path("config"), "product", lambda: False)
    assert result.checks[0].assessment is Assessment.NOT_TESTED
    assert "selector differs" in result.checks[0].explanation
