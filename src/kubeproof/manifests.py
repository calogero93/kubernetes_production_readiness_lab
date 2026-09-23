"""Safe parsing and resource traversal for rendered Kubernetes YAML."""

from __future__ import annotations

import base64
import copy
import hashlib
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import yaml

from kubeproof.domain import ResourceRef


class ManifestError(ValueError):
    pass


MAX_DOCUMENTS = 5_000
MAX_WORKLOADS = 500
WORKLOAD_KINDS = {
    "Pod",
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "ReplicaSet",
    "ReplicationController",
    "Job",
    "CronJob",
}


class KubernetesSafeLoader(yaml.SafeLoader):
    """Accept the literal '=' emitted in Kubernetes CRD enum lists."""


KubernetesSafeLoader.add_constructor("tag:yaml.org,2002:value", lambda _loader, _node: "=")


def parse_manifests(content: bytes) -> tuple[dict[str, Any], ...]:
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError("rendered manifest is not UTF-8") from exc
    try:
        raw_documents = yaml.load_all(decoded, Loader=KubernetesSafeLoader)
        documents: list[dict[str, Any]] = []
        workload_count = 0
        for index, document in enumerate(raw_documents, start=1):
            if document is None:
                continue
            if index > MAX_DOCUMENTS:
                raise ManifestError(f"rendered manifest exceeds {MAX_DOCUMENTS} documents")
            if not isinstance(document, dict):
                raise ManifestError(f"document {index} must be a Kubernetes object mapping")
            api_version = document.get("apiVersion")
            kind = document.get("kind")
            metadata = document.get("metadata")
            if not isinstance(api_version, str) or not isinstance(kind, str):
                raise ManifestError(f"document {index} is missing apiVersion or kind")
            if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
                raise ManifestError(f"document {index} is missing metadata.name")
            documents.append(document)
            workload_count += kind in WORKLOAD_KINDS
            if workload_count > MAX_WORKLOADS:
                raise ManifestError(f"rendered manifest exceeds {MAX_WORKLOADS} workloads")
    except yaml.YAMLError as exc:
        raise ManifestError(f"cannot parse rendered manifest: {exc}") from exc
    return tuple(documents)


@dataclass(frozen=True)
class Workload:
    resource: dict[str, Any]
    pod_spec: dict[str, Any]
    ref: ResourceRef


def resource_ref(resource: Mapping[str, Any], container: str | None = None) -> ResourceRef:
    metadata = resource.get("metadata", {})
    assert isinstance(metadata, dict)
    namespace = metadata.get("namespace")
    return ResourceRef(
        api_version=str(resource["apiVersion"]),
        kind=str(resource["kind"]),
        namespace=str(namespace) if namespace is not None else None,
        name=str(metadata["name"]),
        container=container,
    )


def iter_workloads(resources: tuple[dict[str, Any], ...]) -> Iterator[Workload]:
    pod_spec_paths: dict[str, tuple[str, ...]] = {
        "Pod": ("spec",),
        "Deployment": ("spec", "template", "spec"),
        "StatefulSet": ("spec", "template", "spec"),
        "DaemonSet": ("spec", "template", "spec"),
        "ReplicaSet": ("spec", "template", "spec"),
        "ReplicationController": ("spec", "template", "spec"),
        "Job": ("spec", "template", "spec"),
        "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
    }
    for resource in resources:
        path = pod_spec_paths.get(str(resource.get("kind")))
        if path is None:
            continue
        current: Any = resource
        for segment in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(segment)
        if isinstance(current, dict):
            yield Workload(resource=resource, pod_spec=current, ref=resource_ref(resource))


def iter_containers(workload: Workload) -> Iterator[tuple[str, dict[str, Any]]]:
    for field in ("containers", "initContainers", "ephemeralContainers"):
        containers = workload.pod_spec.get(field, [])
        if not isinstance(containers, list):
            continue
        for container in containers:
            if isinstance(container, dict) and isinstance(container.get("name"), str):
                yield field, container


def redact_manifest(resources: tuple[dict[str, Any], ...]) -> str:
    """Serialize artifacts while retaining hashes, not values, for Secret payloads."""
    redacted = copy.deepcopy(resources)
    for resource in redacted:
        if resource.get("kind") != "Secret":
            continue
        for field in ("data", "stringData"):
            values = resource.get(field)
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                raw = str(value).encode()
                if field == "data":
                    with suppress(ValueError):
                        raw = base64.b64decode(raw, validate=True)
                digest = hashlib.sha256(raw).hexdigest()
                values[key] = f"<redacted:sha256:{digest}>"
    return yaml.safe_dump_all(redacted, sort_keys=True)
