from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from kubeproof.core.domain import Assessment, ExecutionStatus, Observation, SourceClass
from kubeproof.core.profile import CompanyProfile
from kubeproof.execution.environment import EnvironmentError
from kubeproof.execution.helm import HelmRenderError, HelmRenderRequest
from kubeproof.execution.kubernetes import KubernetesError
from kubeproof.execution.runtime import (
    _measure_resources,
    _MetricsSnapshot,
    _observe_dns,
    run_runtime,
)


class FakeKind:
    def __init__(self, *, fail_create: bool = False, fail_delete: bool = False) -> None:
        self.fail_create = fail_create
        self.fail_delete = fail_delete
        self.deleted: list[str] = []

    def create(self, name: str, kubeconfig: Path) -> None:
        if self.fail_create:
            raise EnvironmentError("Docker unavailable")
        kubeconfig.write_text("fake")

    def delete(self, name: str) -> None:
        self.deleted.append(name)
        if self.fail_delete:
            raise EnvironmentError("Docker stopped during cleanup")


class FakeHelm:
    def __init__(self, *, fail_install: bool = False) -> None:
        self.fail_install = fail_install
        self.uninstalled = False

    def install(
        self,
        request: HelmRenderRequest,
        kubeconfig: Path,
        timeout: int,
        cancel_if: Any = None,
    ) -> None:
        if self.fail_install:
            raise HelmRenderError("chart hook failed")

    def uninstall(self, release_name: str, namespace: str, kubeconfig: Path) -> None:
        self.uninstalled = True


class FakeCluster:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self._events = events or []

    def healthy(self) -> bool:
        return True

    def deployments(self) -> list[dict[str, Any]]:
        return []

    def pods(self, namespace: str) -> list[dict[str, Any]]:
        return []

    def pod_metrics(self, namespace: str) -> list[dict[str, Any]]:
        return []

    def events(self, namespace: str) -> list[dict[str, Any]]:
        return self._events

    def product_logs(self, namespace: str) -> dict[str, str]:
        return {}

    def enable_dns_query_logs(self) -> None:
        raise KubernetesError("CoreDNS instrumentation unavailable")


def _run(
    profile: CompanyProfile,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: FakeKind,
    helm: FakeHelm,
) -> Any:
    monkeypatch.setattr("kubeproof.execution.runtime.install_metrics_server", lambda *_: None)
    return run_runtime(
        request=HelmRenderRequest(chart="chart.tgz"),
        resources=(),
        profile=profile,
        install_timeout_seconds=30,
        steady_state_seconds=0,
        max_recovery_targets=0,
        provider=kind,  # type: ignore[arg-type]
        installer=helm,  # type: ignore[arg-type]
        cluster_factory=lambda _: FakeCluster(),  # type: ignore[arg-type]
    )


def test_partial_kind_creation_still_attempts_cleanup(
    strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = FakeKind(fail_create=True)
    result = _run(strict_profile, monkeypatch, kind=kind, helm=FakeHelm())

    assert kind.deleted == [result.environment.cluster_name]
    assert result.environment.cleanup_succeeded
    assert all(
        check.execution_status is ExecutionStatus.INFRASTRUCTURE_FAILURE for check in result.checks
    )
    assert not result.findings


def test_healthy_cluster_with_failed_helm_install_is_product_failure(
    strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = FakeKind()
    helm = FakeHelm(fail_install=True)
    result = _run(strict_profile, monkeypatch, kind=kind, helm=helm)

    assert result.checks[0].assessment is Assessment.FAIL
    assert result.findings[0].observation_ids
    assert all(check.assessment is Assessment.NOT_TESTED for check in result.checks[1:])
    assert helm.uninstalled
    assert kind.deleted == [result.environment.cluster_name]


def test_cleanup_failure_does_not_rewrite_product_evidence(
    strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(
        strict_profile,
        monkeypatch,
        kind=FakeKind(fail_delete=True),
        helm=FakeHelm(fail_install=True),
    )

    assert result.checks[0].assessment is Assessment.FAIL
    assert result.findings[0].observation_ids
    assert result.environment.cleanup_attempted
    assert not result.environment.cleanup_succeeded
    assert "Docker stopped during cleanup" in (result.environment.cleanup_error or "")


def test_image_pull_failure_remains_infrastructure_uncertainty(
    strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kubeproof.execution.runtime.install_metrics_server", lambda *_: None)
    result = run_runtime(
        request=HelmRenderRequest(chart="chart.tgz"),
        resources=(),
        profile=strict_profile,
        install_timeout_seconds=30,
        steady_state_seconds=0,
        max_recovery_targets=0,
        provider=FakeKind(),  # type: ignore[arg-type]
        installer=FakeHelm(fail_install=True),  # type: ignore[arg-type]
        cluster_factory=lambda _: FakeCluster([{"reason": "FailedPull"}]),  # type: ignore[arg-type]
    )

    assert result.checks[0].execution_status is ExecutionStatus.INFRASTRUCTURE_FAILURE
    assert result.checks[0].assessment is Assessment.NOT_TESTED
    assert not result.findings


def test_dynamically_created_unsafe_pod_cancels_install_and_cleans_up(
    strict_profile: CompanyProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnsafeCluster(FakeCluster):
        removed = False

        def pods(self, namespace: str) -> list[dict[str, Any]]:
            if self.removed:
                return []
            return [
                {
                    "metadata": {"name": "unsafe", "namespace": namespace},
                    "spec": {"hostNetwork": True, "containers": [{"name": "app", "image": "x"}]},
                }
            ]

    class CancellableHelm(FakeHelm):
        def install(
            self,
            request: HelmRenderRequest,
            kubeconfig: Path,
            timeout: int,
            cancel_if: Any = None,
        ) -> None:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if cancel_if():
                    raise HelmRenderError("cancelled")
                time.sleep(0.01)
            raise AssertionError("dynamic safety monitor did not cancel Helm")

        def uninstall(self, release_name: str, namespace: str, kubeconfig: Path) -> None:
            unsafe_cluster.removed = True
            super().uninstall(release_name, namespace, kubeconfig)

    monkeypatch.setattr("kubeproof.execution.runtime.install_metrics_server", lambda *_: None)
    kind = FakeKind()
    unsafe_cluster = UnsafeCluster()
    result = run_runtime(
        request=HelmRenderRequest(chart="chart.tgz"),
        resources=(),
        profile=strict_profile,
        install_timeout_seconds=30,
        steady_state_seconds=0,
        max_recovery_targets=0,
        provider=kind,  # type: ignore[arg-type]
        installer=CancellableHelm(),  # type: ignore[arg-type]
        cluster_factory=lambda _: unsafe_cluster,  # type: ignore[arg-type]
    )

    assert all(check.execution_status is ExecutionStatus.CANCELLED for check in result.checks)
    assert not result.findings
    assert "KP-SAFE-002" in result.environment.runtime_safety_rule_ids
    assert kind.deleted == [result.environment.cluster_name]


def test_localhost_dns_query_is_not_reported_as_public_egress(
    strict_profile: CompanyProfile,
) -> None:
    class DNSCluster:
        def coredns_logs(self) -> str:
            return (
                '[INFO] 10.244.0.9:42000 - 123 "A IN _grpclb._tcp.localhost. udp 40 false 512" '
                "NXDOMAIN qr,rd,ra 40 0.001s\n"
                '[INFO] 10.244.0.9:42001 - 124 "A IN api.vendor.example. udp 40 false 512" '
                "NOERROR qr,rd,ra 40 0.001s\n"
            )

        def pod_ip_map(self) -> dict[str, tuple[str, str, str]]:
            return {"10.244.0.9": ("product", "repo", "pod-uid")}

    observations: list[Observation] = []

    def observe(check_id: str, observation_type: str, summary: str, **kwargs: Any) -> Observation:
        item = Observation(
            id=f"obs-{len(observations)}",
            check_id=check_id,
            source_class=SourceClass.RUNTIME,
            observation_type=observation_type,
            summary=summary,
            **kwargs,
        )
        observations.append(item)
        return item

    checks: dict[str, Any] = {}
    findings: list[Any] = []
    _observe_dns(
        DNSCluster(),
        "product",
        strict_profile,
        observe,
        checks,
        findings,
        {},  # type: ignore[arg-type]
    )

    assert [item.data["domain"] for item in observations] == ["api.vendor.example"]
    assert len(findings) == 1
    assert checks["runtime.network"].assessment is Assessment.WARNING


def test_memory_threshold_uses_complete_pod_sample(
    strict_profile_data: dict[str, Any],
) -> None:
    strict_profile_data["constraints"]["resources"]["max_sampled_memory_per_pod"] = "150Mi"
    profile = CompanyProfile.model_validate(strict_profile_data)
    pod = {
        "metadata": {"name": "app-1", "uid": "uid-1"},
        "spec": {"containers": [{"name": "one"}, {"name": "two"}]},
    }
    metrics = {
        "metadata": {"name": "app-1"},
        "timestamp": "2026-09-22T12:00:00Z",
        "window": "15s",
        "containers": [
            {"name": "one", "usage": {"cpu": "10m", "memory": "100Mi"}},
            {"name": "two", "usage": {"cpu": "10m", "memory": "100Mi"}},
        ],
    }
    observations: list[Observation] = []

    def observe(check_id: str, observation_type: str, summary: str, **kwargs: Any) -> Observation:
        item = Observation(
            id=f"obs-{len(observations)}",
            check_id=check_id,
            source_class=SourceClass.RUNTIME,
            observation_type=observation_type,
            summary=summary,
            **kwargs,
        )
        observations.append(item)
        return item

    checks: dict[str, Any] = {}
    findings: list[Any] = []
    _measure_resources(
        [_MetricsSnapshot(10, [pod], [metrics])],
        "product",
        profile,
        0,
        observe,
        checks,
        findings,
    )

    assert checks["runtime.resources"].assessment is Assessment.FAIL
    assert len(findings) == 1
    assert findings[0].constraint == "constraints.resources.max_sampled_memory_per_pod"
    assert observations[0].data["complete"] is True
