"""Docker, Kubernetes and HTTP operations for one real CPU trial."""

from __future__ import annotations

import http.client
import json
import re
import select
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from kubeproof.benchmarks.cpu_http.load import LoadCancelled, run_load
from kubeproof.core.quantities import parse_quantity
from kubeproof.execution.cpu_fixture import FIXTURE_ROOT, IMAGE
from kubeproof.execution.kubernetes import ClusterReader, KubernetesError
from kubeproof.execution.runtime_monitor import UnsafeWorkloadError
from kubeproof.intelligence.models import (
    ConfirmedRequest,
    CpuLoadParameters,
    CpuMeasurement,
    PlanTask,
    TrialRecord,
    TrialStatus,
)
from kubeproof.intelligence.workflow import TrialInfrastructureError

_FORWARD = re.compile(r"^Forwarding from 127\.0\.0\.1:(\d+) -> 8080$")


def _run_command(command: list[str], *, timeout: int) -> None:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KubernetesError(f"local experiment command failed: {command[0]}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace")[-1000:]
        raise KubernetesError(f"local experiment command failed: {command[0]}: {detail}")


def prepare_fixture_image(cluster_name: str) -> None:
    """Build only the local reviewed image and load it into this kind cluster."""
    _run_command(
        ["docker", "build", "-t", IMAGE, "-f", str(FIXTURE_ROOT / "Dockerfile"), str(FIXTURE_ROOT)],
        timeout=300,
    )
    _run_command(["kind", "load", "docker-image", IMAGE, "--name", cluster_name], timeout=180)


@contextmanager
def _forward_pod(kubeconfig: Path, namespace: str, pod_name: str) -> Iterator[int]:
    command = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "-n",
        namespace,
        "port-forward",
        "--address",
        "127.0.0.1",
        f"pod/{pod_name}",
        ":8080",
    ]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except OSError as exc:
        raise KubernetesError("kubectl port-forward could not start") from exc
    try:
        if process.stdout is None:
            raise KubernetesError("kubectl port-forward has no output stream")
        deadline = time.monotonic() + 30
        port: int | None = None
        while time.monotonic() < deadline and process.poll() is None:
            readable, _, _ = select.select([process.stdout], [], [], 0.5)
            if not readable:
                continue
            line = process.stdout.readline().strip()
            match = _FORWARD.fullmatch(line)
            if match:
                port = int(match.group(1))
                break
        if port is None:
            raise KubernetesError("kubectl could not forward the selected Pod within 30 seconds")
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()


def _ready_fixture_pod(cluster: ClusterReader, namespace: str) -> tuple[str, str]:
    pods = cluster.pods(namespace, "app.kubernetes.io/name=cpu-fixture")
    for pod in sorted(pods, key=lambda item: str(item.get("metadata", {}).get("name", ""))):
        metadata = pod.get("metadata", {})
        ready = any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in pod.get("status", {}).get("conditions", [])
        )
        if ready and metadata.get("name") and metadata.get("uid"):
            return str(metadata["name"]), str(metadata["uid"])
    raise KubernetesError("no ready CPU fixture Pod was found")


def _health(port: int) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        response.read(1024)
        if response.status != 200:
            raise KubernetesError("fixture endpoint was not healthy before load")
    except (OSError, http.client.HTTPException) as exc:
        raise KubernetesError("fixture endpoint was unreachable before load") from exc
    finally:
        connection.close()


class _CpuSamples:
    """Collect Metrics API samples while the load generator is running."""

    def __init__(self, cluster: ClusterReader, namespace: str, pod_name: str, pod_uid: str):
        self.cluster = cluster
        self.namespace = namespace
        self.pod_name = pod_name
        self.pod_uid = pod_uid
        self.samples: list[dict[str, Any]] = []
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._collect, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=20)
        if self._thread.is_alive():
            self.error = "CPU sampler did not stop"

    def _collect(self) -> None:
        while not self._stop.is_set():
            try:
                pods = {
                    str(p.get("metadata", {}).get("name")): p
                    for p in self.cluster.pods(self.namespace)
                }
                if pods.get(self.pod_name, {}).get("metadata", {}).get("uid") != self.pod_uid:
                    self.error = "target Pod changed during the trial"
                    return
                for item in self.cluster.pod_metrics(self.namespace):
                    if item.get("metadata", {}).get("name") != self.pod_name:
                        continue
                    containers = item.get("containers", [])
                    if not containers or any("cpu" not in c.get("usage", {}) for c in containers):
                        continue
                    cpu = sum(
                        (parse_quantity(str(c["usage"]["cpu"])) for c in containers),
                        start=Decimal(0),
                    )
                    self.samples.append(
                        {
                            "timestamp": item.get("timestamp"),
                            "window": item.get("window"),
                            "pod_uid": self.pod_uid,
                            "cpu_millicores": int(
                                (cpu * 1000).to_integral_value(rounding=ROUND_CEILING)
                            ),
                            "collected_at": datetime.now(UTC).isoformat(),
                        }
                    )
            except Exception as exc:
                self.error = str(exc)
                return
            self._stop.wait(5)


class CpuLoadWorker:
    """Execute one admitted load task and retain the raw evidence for the report."""

    def __init__(
        self,
        cluster: ClusterReader,
        kubeconfig: Path,
        namespace: str,
        cancel_if: Callable[[], bool],
    ) -> None:
        self.cluster = cluster
        self.kubeconfig = kubeconfig
        self.namespace = namespace
        self.cancel_if = cancel_if
        self.artifacts: dict[str, str] = {}
        self.pod_names: dict[tuple[str, int], str] = {}

    def execute(self, request: ConfirmedRequest, task: PlanTask, attempt: int) -> TrialRecord:
        parameters = CpuLoadParameters.model_validate(task.parameters)
        if self.cancel_if():
            raise UnsafeWorkloadError("runtime safety monitor stopped CPU load")
        try:
            pod_name, pod_uid = _ready_fixture_pod(self.cluster, self.namespace)
            with _forward_pod(self.kubeconfig, self.namespace, pod_name) as port:
                _health(port)
                sampler = _CpuSamples(self.cluster, self.namespace, pod_name, pod_uid)
                sampler.start()
                try:
                    summary = run_load(
                        f"http://127.0.0.1:{port}/work",
                        target_rps=parameters.offered_rps,
                        requests=parameters.requests,
                        work_iterations=request.work_iterations,
                        cancel_if=self.cancel_if,
                    )
                finally:
                    sampler.stop()
            if sampler.error:
                raise TrialInfrastructureError(sampler.error)
            started_at = datetime.fromisoformat(summary.started_at)
            fresh = [
                item
                for item in sampler.samples
                if item["timestamp"]
                and datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00"))
                >= started_at
            ]
            if not fresh:
                raise TrialInfrastructureError("no fresh Pod CPU sample was collected during load")
            peak = max(item["cpu_millicores"] for item in fresh)
            artifact = f"artifacts/cpu/{task.id}-attempt-{attempt}.json"
            self.pod_names[(task.id, attempt)] = pod_name
            self.artifacts[artifact] = (
                json.dumps(
                    {
                        "load": asdict(summary),
                        "pod_name": pod_name,
                        "pod_uid": pod_uid,
                        "cpu_samples": fresh,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            measurement = CpuMeasurement(
                achieved_rps=summary.success_rps,
                p95_ms=summary.latency_ms_p95,
                failed_requests=summary.failed + summary.dropped_by_client,
                cpu_millicores=peak,
                cpu_sample_complete=True,
                evidence_ref=artifact,
            )
            return TrialRecord(
                task_id=task.id,
                attempt=attempt,
                status=TrialStatus.COMPLETED,
                observation=measurement.model_dump(mode="json"),
                explanation=f"Real load measured against Pod {pod_name}.",
            )
        except LoadCancelled as exc:
            raise UnsafeWorkloadError(str(exc)) from exc
        except KubernetesError as exc:
            raise TrialInfrastructureError(str(exc)) from exc
