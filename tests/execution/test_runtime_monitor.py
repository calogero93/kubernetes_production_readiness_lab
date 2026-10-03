from __future__ import annotations

from typing import Any

import pytest

from kubeproof.core.profile import CompanyProfile
from kubeproof.execution.runtime_monitor import MetricsCollector, SafetyMonitor


class PodsOnlyCluster:
    def __init__(self, privileged: bool = False) -> None:
        self.privileged = privileged

    def pods(self, namespace: str) -> list[dict[str, Any]]:
        del namespace
        return [
            {
                "metadata": {"name": "dynamic-host-reader"},
                "spec": {
                    "hostNetwork": True,
                    "containers": [
                        {
                            "name": "reader",
                            "image": "example/reader:1",
                            "securityContext": {"privileged": self.privileged},
                        }
                    ],
                },
            }
        ]


def test_dynamic_host_access_is_allowed_only_for_approved_rule(
    strict_profile: CompanyProfile,
) -> None:
    allowed = SafetyMonitor(
        PodsOnlyCluster(),  # type: ignore[arg-type]
        "product",
        strict_profile,
        approved_host_rule_ids=("KP-SAFE-002",),
    )
    allowed.start()
    assert not allowed.triggered.wait(0.05)
    allowed.stop()
    assert not allowed.triggered.is_set()

    blocked = SafetyMonitor(
        PodsOnlyCluster(privileged=True),  # type: ignore[arg-type]
        "product",
        strict_profile,
        approved_host_rule_ids=("KP-SAFE-002",),
    )
    blocked.start()
    assert blocked.triggered.wait(1)
    blocked.stop()
    assert "KP-SAFE-001" in blocked.matched_rule_ids


def test_monitor_rejects_approval_for_non_host_rule(strict_profile: CompanyProfile) -> None:
    with pytest.raises(ValueError, match="only host-access rules"):
        SafetyMonitor(
            PodsOnlyCluster(),  # type: ignore[arg-type]
            "product",
            strict_profile,
            approved_host_rule_ids=("KP-SAFE-001",),
        )


def test_http_instrument_pods_are_excluded_from_product_resource_samples() -> None:
    class Cluster:
        def pods(self, namespace: str) -> list[dict[str, Any]]:
            collector._stop.set()
            return [
                {"metadata": {"name": "api-pod", "labels": {"app": "api"}}},
                {
                    "metadata": {
                        "name": "probe-pod",
                        "labels": {"kubeproof.dev/instrument": "http-service"},
                    }
                },
            ]

        def pod_metrics(self, namespace: str) -> list[dict[str, Any]]:
            return [{"metadata": {"name": name}} for name in ("api-pod", "probe-pod")]

    collector = MetricsCollector(Cluster(), "product")  # type: ignore[arg-type]
    collector._collect()
    assert [pod["metadata"]["name"] for pod in collector.snapshots[0].pods] == ["api-pod"]
    assert [item["metadata"]["name"] for item in collector.snapshots[0].metrics] == ["api-pod"]
