"""The exact Helm workload allowed to receive local CPU load."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kubeproof.execution.helm import HelmRenderRequest
from kubeproof.intelligence.models import ConfirmedRequest

IMAGE = "kubeproof-cpu-fixture:local"
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "cpu_http"


def validate_fixture_resources(
    resources: tuple[dict[str, Any], ...], request: HelmRenderRequest, confirmed: ConfirmedRequest
) -> None:
    """Reject anything beyond the reviewed fixture before creating a cluster."""
    try:
        _validate_fixture_resources(resources, request, confirmed)
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise ValueError("fixture chart has an invalid manifest structure") from exc


def _validate_fixture_resources(
    resources: tuple[dict[str, Any], ...], request: HelmRenderRequest, confirmed: ConfirmedRequest
) -> None:
    if len(resources) != 2 or {item.get("kind") for item in resources} != {
        "Deployment",
        "Service",
    }:
        raise ValueError("live CPU test accepts only the fixture Deployment and Service")
    by_kind = {str(item["kind"]): item for item in resources}
    deployment = by_kind["Deployment"]
    service = by_kind["Service"]
    labels = {
        "app.kubernetes.io/name": "cpu-fixture",
        "app.kubernetes.io/instance": request.release_name,
    }
    for item in resources:
        metadata = item.get("metadata", {})
        if (
            metadata.get("name") != request.release_name
            or metadata.get("namespace") != request.namespace
        ):
            raise ValueError("fixture resources must use the isolated release and namespace")
        if set(metadata) - {"name", "namespace", "labels"} or metadata.get("labels") != labels:
            raise ValueError("fixture resource metadata differs from the reviewed contract")
    deployment_spec = deployment.get("spec", {})
    if set(deployment_spec) != {"replicas", "selector", "template"}:
        raise ValueError("fixture Deployment has unsupported fields")
    if (
        not isinstance(deployment_spec["replicas"], int)
        or not 1 <= deployment_spec["replicas"] <= 3
    ):
        raise ValueError("fixture Deployment must have between one and three replicas")
    if deployment_spec["selector"] != {"matchLabels": labels}:
        raise ValueError("fixture Deployment selector differs from the reviewed contract")
    template = deployment_spec["template"]
    if set(template) != {"metadata", "spec"} or template["metadata"] != {"labels": labels}:
        raise ValueError("fixture Pod template metadata differs from the reviewed contract")
    pod_spec = template["spec"]
    if set(pod_spec) != {"securityContext", "containers"}:
        raise ValueError("fixture Pod has unsupported fields")
    if pod_spec["securityContext"] != {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
    }:
        raise ValueError("fixture Pod security context differs from the reviewed contract")
    containers = pod_spec.get("containers", [])
    if len(containers) != 1 or containers[0].get("image") != IMAGE:
        raise ValueError("fixture Deployment must use the local CPU image")
    if set(containers[0]) != {
        "name",
        "image",
        "imagePullPolicy",
        "args",
        "ports",
        "readinessProbe",
        "livenessProbe",
        "securityContext",
        "resources",
    }:
        raise ValueError("fixture container has unsupported fields")
    args = containers[0].get("args", [])
    if args != ["--host", "0.0.0.0", "--iterations", str(confirmed.work_iterations)]:
        raise ValueError("fixture workIterations must match the confirmed request")
    container = containers[0]
    if (
        container["name"] != "cpu-fixture"
        or container["imagePullPolicy"] != "IfNotPresent"
        or container["ports"] != [{"name": "http", "containerPort": 8080}]
        or container["readinessProbe"] != {"httpGet": {"path": "/healthz", "port": "http"}}
        or container["livenessProbe"] != {"httpGet": {"path": "/healthz", "port": "http"}}
        or container["securityContext"]
        != {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
    ):
        raise ValueError("fixture container differs from the reviewed contract")
    resources_spec = container["resources"]
    if set(resources_spec) != {"limits", "requests"} or any(
        set(resources_spec[key]) != {"cpu", "memory"} for key in resources_spec
    ):
        raise ValueError("fixture container must declare CPU and memory requests and limits")
    if service.get("spec") != {
        "type": "ClusterIP",
        "selector": labels,
        "ports": [{"name": "http", "port": 8080, "targetPort": "http"}],
    }:
        raise ValueError("fixture Service differs from the reviewed contract")
