"""Pinned evaluation infrastructure installed inside a fresh kind cluster."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from kubeproof.kubernetes import ClusterReader, KubernetesError


class InfrastructureError(RuntimeError):
    pass


METRICS_SERVER_MANIFEST = Path(__file__).parent / "assets" / "metrics-server-v0.7.2.yaml"


def install_metrics_server(kubeconfig: Path, cluster: ClusterReader, timeout: int = 120) -> None:
    try:
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "apply",
                "-f",
                str(METRICS_SERVER_MANIFEST),
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfrastructureError(f"cannot apply Metrics Server manifest: {exc}") from exc
    if result.returncode:
        raise InfrastructureError(
            "cannot apply Metrics Server manifest: "
            + result.stderr.decode("utf-8", "replace")[-2000:]
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if cluster.pod_metrics("kube-system"):
                return
        except KubernetesError:
            pass
        time.sleep(5)
    raise InfrastructureError("Metrics API did not provide samples before the deadline")
