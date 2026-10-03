"""A confirmed, bounded Service HTTP experiment contributing sealed evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError as TransportError

from kubeproof.core.domain import (
    Assessment,
    CheckResult,
    ExecutionStatus,
    Finding,
    HttpProbeOptions,
    Observation,
    ResourceRef,
    Severity,
    SourceClass,
)
from kubeproof.execution.helm import HelmRenderRequest
from kubeproof.execution.kubernetes import ClusterReader, KubernetesError
from kubeproof.execution.runtime import ExperimentEvidence
from kubeproof.execution.runtime_checks import untested
from kubeproof.execution.runtime_monitor import UnsafeWorkloadError
from kubeproof.execution.service_probe import (
    ServiceProbeCancelled,
    ServiceProbeError,
    run_service_probe,
)

CHECK_ID = "runtime.http_service"
TITLE = "Bounded HTTP requests to a product Service"
LIMITATION = (
    "Sequential plain HTTP GET requests from a probe Pod in the product namespace. "
    "No redirect following, authentication, TLS, throughput or backend-distribution measurement. "
    "Latency includes failed requests and a three-second socket timeout. Network errors may "
    "reflect Service endpoints, DNS, application behavior or NetworkPolicy; "
    "root cause is not inferred."
)


def _validate_service(service: dict[str, Any], options: HttpProbeOptions) -> None:
    spec = service.get("spec", {})
    if not isinstance(spec, dict):
        raise ValueError("HTTP Service spec must be a mapping")
    if (
        spec.get("type", "ClusterIP") != "ClusterIP"
        or spec.get("clusterIP") == "None"
        or spec.get("externalName")
        or spec.get("externalIPs")
        or not isinstance(spec.get("selector"), dict)
        or not spec["selector"]
    ):
        raise ValueError("HTTP probe requires a selector-based, non-headless ClusterIP Service")
    ports = spec.get("ports")
    if not isinstance(ports, list) or not any(
        isinstance(port, dict)
        and type(port.get("port")) is int
        and port["port"] == options.port
        and port.get("protocol", "TCP") == "TCP"
        for port in ports
    ):
        raise ValueError("HTTP probe port must be a declared TCP Service port")


def _command(command: list[str], timeout: int) -> str:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KubernetesError(f"HTTP probe image preparation failed: {command[0]}: {exc}") from exc
    if result.returncode:
        raise KubernetesError(
            "HTTP probe image preparation failed: "
            + result.stderr.decode("utf-8", "replace")[-1500:]
        )
    return result.stdout.decode("utf-8", "replace").strip()


class HttpServiceExperiment:
    check_id = CHECK_ID
    title = TITLE
    requires_metrics = False

    def __init__(self, options: HttpProbeOptions) -> None:
        self.options = options
        root = Path(__file__).parent
        digest = hashlib.sha256(
            (root / "service_probe_runner.py").read_bytes()
            + (root / "service-probe.Dockerfile").read_bytes()
        ).hexdigest()
        self.image = f"kubeproof-http-probe:{digest[:16]}"
        self.image_id: str | None = None
        self._selector: dict[str, Any] | None = None

    def validate_resources(
        self, resources: tuple[dict[str, Any], ...], request: HelmRenderRequest
    ) -> None:
        targets = [
            resource
            for resource in resources
            if resource.get("kind") == "Service"
            and resource.get("metadata", {}).get("name") == self.options.service
            and (resource.get("metadata", {}).get("namespace") or request.namespace)
            == request.namespace
        ]
        if len(targets) != 1:
            raise ValueError(
                "HTTP probe must select exactly one rendered Service in the product namespace"
            )
        if "helm.sh/hook" in (targets[0]["metadata"].get("annotations") or {}):
            raise ValueError("HTTP probe cannot target a temporary Helm hook Service")
        _validate_service(targets[0], self.options)
        self._selector = dict(targets[0]["spec"]["selector"])

    def prepare(self, cluster_name: str, kubeconfig: Path) -> None:
        del kubeconfig
        root = Path(__file__).parent
        _command(
            [
                "docker",
                "build",
                "--quiet",
                "-t",
                self.image,
                "-f",
                str(root / "service-probe.Dockerfile"),
                str(root),
            ],
            timeout=300,
        )
        self.image_id = _command(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.image], 30
        )
        _command(["kind", "load", "docker-image", self.image, "--name", cluster_name], 180)

    def run(
        self,
        cluster: ClusterReader,
        kubeconfig: Path,
        namespace: str,
        cancel_if: Callable[[], bool],
    ) -> ExperimentEvidence:
        del kubeconfig
        if cancel_if():
            raise UnsafeWorkloadError("HTTP probe cancelled by the runtime safety monitor")
        started_at = datetime.now(UTC)
        try:
            service = cluster.core.read_namespaced_service(
                self.options.service, namespace, _request_timeout=10
            )
            live = cluster.api_client.sanitize_for_serialization(service)
            if not isinstance(live, dict):
                raise ServiceProbeError("live Service could not be validated")
            _validate_service(live, self.options)
            if live["spec"]["selector"] != self._selector:
                raise ServiceProbeError("live Service selector differs from the reviewed manifest")
            result = run_service_probe(
                service=self.options.service,
                namespace=namespace,
                port=self.options.port,
                path=self.options.path,
                requests=self.options.requests,
                image=self.image,
                api_client=cluster.api_client,
                cancel_if=cancel_if,
            )
            if result["schema_version"] != "2":
                raise ServiceProbeError(
                    "HTTP experiment requires per-request timings from probe schema 2"
                )
        except ServiceProbeCancelled as exc:
            raise UnsafeWorkloadError(str(exc)) from exc
        except (ServiceProbeError, ValueError, ApiException, OSError, TransportError) as exc:
            return ExperimentEvidence(
                (untested(self.check_id, self.title, str(exc), failed=True),),
                (),
                (),
                {"artifacts/http/error.txt": str(exc) + "\n"},
            )
        failed = self.options.requests - result["outcomes"].get(
            str(self.options.expected_status), 0
        )
        passed = failed <= self.options.max_failed_requests and (
            self.options.max_p95_ms is None or result["p95_ms"] <= self.options.max_p95_ms
        )
        p95_limit = self.options.max_p95_ms if self.options.max_p95_ms is not None else "unset"
        observation = Observation(
            id="http-obs-00001",
            check_id=self.check_id,
            source_class=SourceClass.RUNTIME,
            observation_type="http.service_probe",
            summary=(
                f"Service GET probe returned {self.options.requests - failed}/"
                f"{self.options.requests} expected responses; p95 {result['p95_ms']} ms."
            ),
            resource=ResourceRef(
                api_version="v1",
                kind="Service",
                namespace=namespace,
                name=self.options.service,
            ),
            data={
                "parameters": self.options.model_dump(mode="json"),
                "measurement": result,
                "failed_requests": failed,
                "meets_goal": passed,
                "probe_image": self.image,
                "probe_image_id": self.image_id,
                "origin_namespace": namespace,
                "experiment_started_at": started_at.isoformat(),
                "experiment_completed_at": datetime.now(UTC).isoformat(),
                "limitation": LIMITATION,
            },
        )
        findings = (
            ()
            if passed
            else (
                Finding(
                    id="http-finding-00001",
                    check_id=self.check_id,
                    severity=Severity.BLOCKER,
                    title="HTTP Service probe did not meet confirmed requirements",
                    description=(
                        f"Observed {failed} unexpected responses or network errors "
                        f"(allowed {self.options.max_failed_requests}); p95 {result['p95_ms']} ms "
                        f"(maximum {p95_limit})."
                    ),
                    observation_ids=(observation.id,),
                    constraint="execution_options.http_probe",
                    remediation=(
                        "Review the endpoint, Service selector, application logs and NetworkPolicy."
                    ),
                    limitation=LIMITATION,
                ),
            )
        )
        check = CheckResult(
            id=self.check_id,
            title=self.title,
            execution_status=ExecutionStatus.COMPLETED,
            assessment=Assessment.PASS if passed else Assessment.FAIL,
            observation_ids=(observation.id,),
            finding_ids=tuple(finding.id for finding in findings),
            explanation=LIMITATION,
        )
        return ExperimentEvidence(
            (check,),
            (observation,),
            findings,
            {
                "artifacts/http/service-probe.json": json.dumps(
                    observation.data, indent=2, sort_keys=True
                )
                + "\n"
            },
        )
