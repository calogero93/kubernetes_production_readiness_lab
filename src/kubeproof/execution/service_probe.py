"""Launch a short-lived in-cluster Service probe and collect its result."""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError as TransportError

DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
HTTP_PATH = re.compile(r"^/[a-zA-Z0-9/_.~-]*$")


class ServiceProbeError(RuntimeError):
    """The bounded probe could not produce an observation."""


class ServiceProbeCancelled(ServiceProbeError):
    """The caller cancelled the probe before a complete observation was obtained."""


def validate_result(
    result: object, *, service: str, namespace: str, port: int, path: str, requests: int
) -> dict[str, Any]:
    target = f"http://{service}.{namespace}.svc:{port}{path}"
    if not isinstance(result, dict):
        raise ServiceProbeError("probe Job returned an invalid observation")
    fields = {
        "schema_version",
        "target",
        "requests",
        "outcomes",
        "p95_ms",
    }
    if result.get("schema_version") == "2":
        fields.add("latencies_ms")
        success_key = "all_responses_200"
    else:
        success_key = "passed"
    fields.add(success_key)
    if set(result) != fields:
        raise ServiceProbeError("probe Job returned an invalid observation")
    outcomes = result["outcomes"]
    if (
        result["schema_version"] not in ("1", "2")
        or result["target"] != target
        or type(result["requests"]) is not int
        or result["requests"] != requests
        or not isinstance(outcomes, dict)
        or not outcomes
        or any(
            not isinstance(key, str)
            or (key != "network_error" and not re.fullmatch(r"[1-5][0-9]{2}", key))
            or type(count) is not int
            or count < 0
            for key, count in outcomes.items()
        )
        or sum(outcomes.values()) != requests
        or type(result["p95_ms"]) not in (int, float)
        or not math.isfinite(result["p95_ms"])
        or result["p95_ms"] < 0
        or type(result[success_key]) is not bool
        or result[success_key] != (outcomes == {"200": requests})
    ):
        raise ServiceProbeError("probe Job returned an invalid observation")
    if result["schema_version"] == "2":
        latencies = result["latencies_ms"]
        if (
            not isinstance(latencies, list)
            or len(latencies) != requests
            or any(
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
                for value in latencies
            )
            or result["p95_ms"] != round(sorted(latencies)[math.ceil(requests * 0.95) - 1], 3)
        ):
            raise ServiceProbeError("probe Job returned inconsistent request timings")
    return result


def job_manifest(
    *, name: str, namespace: str, service: str, port: int, path: str, requests: int, image: str
) -> dict[str, Any]:
    if not all(DNS_LABEL.fullmatch(value) and len(value) <= 63 for value in (namespace, service)):
        raise ValueError("service and namespace must be Kubernetes DNS labels")
    if not HTTP_PATH.fullmatch(path) or "//" in path or len(path) > 256:
        raise ValueError("path must be an HTTP path without a query string")
    if not 1 <= port <= 65535 or not 1 <= requests <= 50:
        raise ValueError("port must be 1-65535 and requests must be 1-50")
    if not image or any(char.isspace() for char in image):
        raise ValueError("a pullable KubeProof image reference is required")
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 180,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "kubeproof-service-probe",
                        "kubeproof.dev/instrument": "http-service",
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "probe",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "-m", "kubeproof.execution.service_probe_runner"],
                            "args": [service, namespace, str(port), path, str(requests)],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "20m", "memory": "64Mi"},
                                "limits": {"cpu": "200m", "memory": "128Mi"},
                            },
                        }
                    ],
                },
            },
        },
    }


def run_service_probe(
    *,
    service: str,
    namespace: str,
    port: int,
    path: str,
    requests: int,
    image: str,
    context: str | None = None,
    api_client: client.ApiClient | None = None,
    cancel_if: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Requires namespace-scoped get/create/list/delete Job and get Pod/log rights."""
    name = f"kubeproof-probe-{uuid.uuid4().hex[:12]}"
    manifest = job_manifest(
        name=name,
        namespace=namespace,
        service=service,
        port=port,
        path=path,
        requests=requests,
        image=image,
    )
    owned_client = api_client is None
    creation_attempted = False
    batch = None
    if cancel_if is not None and cancel_if():
        raise ServiceProbeCancelled("Service probe cancelled before Job creation")
    try:
        try:
            if api_client is None:
                api_client = config.new_client_from_config(context=context)
            batch = client.BatchV1Api(api_client)
            core = client.CoreV1Api(api_client)
            core.read_namespaced_service(service, namespace, _request_timeout=10)
            creation_attempted = True
            batch.create_namespaced_job(namespace, manifest, _request_timeout=15)
        except (ApiException, OSError, ConfigException, TransportError) as exc:
            raise ServiceProbeError(f"cannot start Service probe: {exc}") from exc
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if cancel_if is not None and cancel_if():
                raise ServiceProbeCancelled("Service probe cancelled by the runtime safety monitor")
            status = batch.read_namespaced_job_status(name, namespace, _request_timeout=10).status
            if status.succeeded:
                pods = core.list_namespaced_pod(
                    namespace, label_selector=f"job-name={name}", _request_timeout=10
                ).items
                if not pods or not pods[0].metadata or not pods[0].metadata.name:
                    raise ServiceProbeError("completed probe Job has no readable Pod")
                response = core.read_namespaced_pod_log(
                    pods[0].metadata.name,
                    namespace,
                    _request_timeout=10,
                    _preload_content=False,
                    limit_bytes=64 * 1024,
                )
                try:
                    result = json.loads(response.data)
                except json.JSONDecodeError as exc:
                    raise ServiceProbeError("probe Job returned invalid JSON") from exc
                finally:
                    response.close()
                if cancel_if is not None and cancel_if():
                    raise ServiceProbeCancelled(
                        "Service probe cancelled before collecting its result"
                    )
                return validate_result(
                    result,
                    service=service,
                    namespace=namespace,
                    port=port,
                    path=path,
                    requests=requests,
                )
            if status.failed:
                raise ServiceProbeError(
                    "probe Job failed before complete measurements were obtained"
                )
            time.sleep(2)
        raise ServiceProbeError("probe Job exceeded its 180-second deadline")
    except (ApiException, OSError, TransportError) as exc:
        raise ServiceProbeError(f"cannot read Service probe result: {exc}") from exc
    finally:
        if creation_attempted and batch is not None:
            with suppress(ApiException, OSError, TransportError):
                batch.delete_namespaced_job(
                    name, namespace, propagation_policy="Background", _request_timeout=10
                )
        if owned_client and api_client is not None:
            api_client.close()
