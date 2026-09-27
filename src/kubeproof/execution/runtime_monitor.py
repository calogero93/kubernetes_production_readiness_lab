"""Background observers used only during one disposable kind run."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from kubeproof.core.analysis import analyze
from kubeproof.core.domain import AdmissionOutcome
from kubeproof.core.profile import CompanyProfile
from kubeproof.core.safety import decide_local_admission
from kubeproof.execution.kubernetes import ClusterReader


@dataclass(frozen=True)
class MetricsSnapshot:
    captured_at: float
    pods: list[dict[str, Any]]
    metrics: list[dict[str, Any]]


class MetricsCollector:
    """Collect raw Metrics API snapshots while Helm blocks on installation."""

    def __init__(self, cluster: ClusterReader, namespace: str) -> None:
        self.cluster = cluster
        self.namespace = namespace
        self.snapshots: list[MetricsSnapshot] = []
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._collect, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            self.error = "Metrics collector did not stop before its deadline"

    def _collect(self) -> None:
        while not self._stop.is_set():
            try:
                pods = self.cluster.pods(self.namespace)
                metrics = self.cluster.pod_metrics(self.namespace)
                if len(self.snapshots) >= 120:
                    self.error = "Metrics collector exceeded the 120-snapshot safety limit"
                    return
                self.snapshots.append(MetricsSnapshot(time.monotonic(), pods, metrics))
            except Exception as exc:
                self.error = str(exc)
                return
            self._stop.wait(15)


class UnsafeWorkloadError(RuntimeError):
    """The local safety monitor cancelled execution after a dynamic violation."""


class SafetyMonitor:
    """Poll dynamically created Pod specs and stop at a local hard rule."""

    def __init__(self, cluster: ClusterReader, namespace: str, profile: CompanyProfile) -> None:
        self.cluster = cluster
        self.namespace = namespace
        self.profile = profile
        self.triggered = threading.Event()
        self._stop = threading.Event()
        self.reason: str | None = None
        self.infrastructure_error = False
        self.matched_rule_ids: tuple[str, ...] = ()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=20)
        if self._thread.is_alive():
            self.infrastructure_error = True
            self.reason = "dynamic workload monitor did not stop before its deadline"
            self.triggered.set()

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                for pod in self.cluster.pods(self.namespace):
                    metadata = pod.get("metadata", {})
                    manifest = {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": {
                            "name": str(metadata.get("name", "unknown")),
                            "namespace": self.namespace,
                        },
                        "spec": pod.get("spec", {}),
                    }
                    decision = decide_local_admission(
                        analyze((manifest,), self.profile).observations
                    )
                    if decision.outcome is AdmissionOutcome.STATIC_ONLY:
                        self.matched_rule_ids = decision.matched_rule_ids
                        self.reason = (
                            f"dynamic Pod {metadata.get('name')} matched local safety rules: "
                            + ", ".join(decision.matched_rule_ids)
                        )
                        self.triggered.set()
                        return
            except Exception as exc:
                self.infrastructure_error = True
                self.reason = f"dynamic workload monitor failed: {exc}"
                self.triggered.set()
                return
            self._stop.wait(2)
