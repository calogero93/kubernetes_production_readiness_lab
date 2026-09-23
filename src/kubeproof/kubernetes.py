"""Narrow Kubernetes API reads for a disposable evaluation cluster."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException


class KubernetesError(RuntimeError):
    pass


def _plain(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KubernetesError("Kubernetes API returned an unexpected response")
    return value


class ClusterReader:
    def __init__(self, kubeconfig: Path) -> None:
        configuration = client.Configuration()
        config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
        self.api_client = client.ApiClient(configuration)
        self.core = client.CoreV1Api(self.api_client)
        self.apps = client.AppsV1Api(self.api_client)
        self.batch = client.BatchV1Api(self.api_client)
        self.custom = client.CustomObjectsApi(self.api_client)
        self.version_api = client.VersionApi(self.api_client)

    def server_version(self) -> str:
        try:
            return str(self.version_api.get_code(_request_timeout=10).git_version)
        except (ApiException, OSError) as exc:
            raise KubernetesError(f"cannot read Kubernetes server version: {exc}") from exc

    def healthy(self) -> bool:
        try:
            self.core.list_node(_request_timeout=10)
            return True
        except (ApiException, OSError):
            return False

    def deployments(self) -> list[dict[str, Any]]:
        try:
            response = self.apps.list_deployment_for_all_namespaces(_request_timeout=15)
            return [
                _plain(self.api_client.sanitize_for_serialization(item)) for item in response.items
            ]
        except (ApiException, OSError) as exc:
            raise KubernetesError(f"cannot list Deployments: {exc}") from exc

    def statefulsets(self) -> list[dict[str, Any]]:
        try:
            response = self.apps.list_stateful_set_for_all_namespaces(_request_timeout=15)
            return [
                _plain(self.api_client.sanitize_for_serialization(item)) for item in response.items
            ]
        except (ApiException, OSError) as exc:
            raise KubernetesError(f"cannot list StatefulSets: {exc}") from exc

    def daemonsets(self) -> list[dict[str, Any]]:
        try:
            response = self.apps.list_daemon_set_for_all_namespaces(_request_timeout=15)
            return [
                _plain(self.api_client.sanitize_for_serialization(item)) for item in response.items
            ]
        except (ApiException, OSError) as exc:
            raise KubernetesError(f"cannot list DaemonSets: {exc}") from exc

    def jobs(self) -> list[dict[str, Any]]:
        try:
            response = self.batch.list_job_for_all_namespaces(_request_timeout=15)
            return [
                _plain(self.api_client.sanitize_for_serialization(item)) for item in response.items
            ]
        except (ApiException, OSError) as exc:
            raise KubernetesError(f"cannot list Jobs: {exc}") from exc

    def pods(self, namespace: str, selector: str = "") -> list[dict[str, Any]]:
        try:
            response = self.core.list_namespaced_pod(
                namespace, label_selector=selector, _request_timeout=15
            )
            if len(response.items) > 500:
                raise KubernetesError("product namespace exceeds the 500 Pod safety limit")
            return [
                _plain(self.api_client.sanitize_for_serialization(item)) for item in response.items
            ]
        except ApiException as exc:
            if exc.status == 404:
                return []
            raise KubernetesError(f"cannot list Pods: {exc}") from exc
        except OSError as exc:
            raise KubernetesError(f"cannot list Pods: {exc}") from exc

    def delete_pod(self, name: str, namespace: str) -> None:
        try:
            self.core.delete_namespaced_pod(name, namespace, _request_timeout=15)
        except (ApiException, OSError) as exc:
            raise KubernetesError(f"cannot delete Pod {namespace}/{name}: {exc}") from exc

    def pod_metrics(self, namespace: str) -> list[dict[str, Any]]:
        try:
            response = self.custom.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods", _request_timeout=15
            )
            return [_plain(item) for item in _plain(response).get("items", [])]
        except ApiException as exc:
            if exc.status == 404:
                return []
            raise KubernetesError(f"Metrics API unavailable: {exc}") from exc
        except OSError as exc:
            raise KubernetesError(f"Metrics API unavailable: {exc}") from exc

    def coredns_logs(self) -> str:
        try:
            pods = self.core.list_namespaced_pod(
                "kube-system", label_selector="k8s-app=kube-dns", _request_timeout=15
            ).items
            return "\n".join(
                self.core.read_namespaced_pod_log(
                    pod.metadata.name, "kube-system", tail_lines=3000, _request_timeout=15
                )
                for pod in pods
                if pod.metadata and pod.metadata.name
            )
        except (ApiException, OSError) as exc:
            raise KubernetesError(f"cannot read CoreDNS logs: {exc}") from exc

    def enable_dns_query_logs(self) -> None:
        try:
            configmap = self.core.read_namespaced_config_map(
                "coredns", "kube-system", _request_timeout=15
            )
            corefile = (configmap.data or {}).get("Corefile", "")
            if ".:53 {" not in corefile:
                raise KubernetesError("CoreDNS Corefile has no expected root server block")
            if "\n    log\n" not in corefile:
                new_corefile = corefile.replace(".:53 {", ".:53 {\n    log", 1)
                self.core.patch_namespaced_config_map(
                    "coredns",
                    "kube-system",
                    {"data": {"Corefile": new_corefile}},
                    _request_timeout=15,
                )
                deployment = self.apps.read_namespaced_deployment(
                    "coredns", "kube-system", _request_timeout=15
                )
                if deployment.spec and deployment.spec.template.metadata:
                    annotations = deployment.spec.template.metadata.annotations or {}
                    annotations["kubeproof.dev/restarted-at"] = datetime.now(UTC).isoformat()
                    self.apps.patch_namespaced_deployment(
                        "coredns",
                        "kube-system",
                        {"spec": {"template": {"metadata": {"annotations": annotations}}}},
                        _request_timeout=15,
                    )
        except (ApiException, OSError) as exc:
            raise KubernetesError(f"cannot enable CoreDNS query logs: {exc}") from exc

    def pod_ip_map(self) -> dict[str, tuple[str, str, str]]:
        try:
            response = self.core.list_pod_for_all_namespaces(_request_timeout=15)
            return {
                str(pod.status.pod_ip): (
                    str(pod.metadata.namespace),
                    str(pod.metadata.name),
                    str(pod.metadata.uid),
                )
                for pod in response.items
                if pod.status and pod.status.pod_ip and pod.metadata
            }
        except (ApiException, OSError) as exc:
            raise KubernetesError(f"cannot map Pod IPs: {exc}") from exc

    def events(self, namespace: str) -> list[dict[str, Any]]:
        try:
            response = self.core.list_namespaced_event(namespace, _request_timeout=15)
            return [
                _plain(self.api_client.sanitize_for_serialization(item))
                for item in response.items[:500]
            ]
        except (ApiException, OSError) as exc:
            raise KubernetesError(f"cannot list Events: {exc}") from exc

    def product_logs(self, namespace: str) -> dict[str, str]:
        result: dict[str, str] = {}
        total_bytes = 0
        for pod in self.pods(namespace)[:100]:
            pod_name = str(pod.get("metadata", {}).get("name", ""))
            for container in pod.get("spec", {}).get("containers", [])[:10]:
                container_name = str(container.get("name", ""))
                try:
                    value = self.core.read_namespaced_pod_log(
                        pod_name,
                        namespace,
                        container=container_name,
                        tail_lines=200,
                        _request_timeout=10,
                    )
                except (ApiException, OSError):
                    continue
                excerpt = str(value)[-100_000:]
                total_bytes += len(excerpt.encode("utf-8"))
                if total_bytes > 1_000_000:
                    return result
                result[f"{pod_name}/{container_name}"] = excerpt
        return result


def wait_for_deployments(
    cluster: ClusterReader,
    names: set[tuple[str, str]],
    *,
    timeout: int,
    poll_seconds: float = 3,
) -> tuple[bool, list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout
    latest: list[dict[str, Any]] = []
    while True:
        latest = [
            item
            for item in cluster.deployments()
            if (item.get("metadata", {}).get("namespace"), item.get("metadata", {}).get("name"))
            in names
        ]
        ready = len(latest) == len(names) and all(
            int(item.get("status", {}).get("readyReplicas") or 0)
            >= int(item.get("spec", {}).get("replicas") or 1)
            and int(item.get("status", {}).get("observedGeneration") or 0)
            >= int(item.get("metadata", {}).get("generation") or 0)
            for item in latest
        )
        if ready:
            return True, latest
        if time.monotonic() >= deadline:
            return False, latest
        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))


def wait_for_workloads(
    cluster: ClusterReader,
    names: set[tuple[str, str, str]],
    *,
    timeout: int,
    poll_seconds: float = 3,
) -> tuple[bool, list[dict[str, Any]]]:
    """Wait for supported rendered workload controllers, excluding Helm hooks."""
    readers = {
        "Deployment": cluster.deployments,
        "StatefulSet": cluster.statefulsets,
        "DaemonSet": cluster.daemonsets,
        "Job": cluster.jobs,
    }
    deadline = time.monotonic() + timeout
    latest: list[dict[str, Any]] = []
    while True:
        current = {
            (
                kind,
                item.get("metadata", {}).get("namespace"),
                item.get("metadata", {}).get("name"),
            ): item
            for kind, reader in readers.items()
            if any(name[0] == kind for name in names)
            for item in reader()
        }
        latest = [current[name] for name in sorted(names) if name in current]
        ready = len(latest) == len(names) and all(
            _workload_ready(kind, current[(kind, namespace, name)])
            for kind, namespace, name in names
        )
        if ready:
            return True, latest
        if time.monotonic() >= deadline:
            return False, latest
        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))


def _workload_ready(kind: str, item: dict[str, Any]) -> bool:
    status = item.get("status", {})
    spec = item.get("spec", {})
    if kind == "Job":
        return int(status.get("succeeded") or 0) >= int(spec.get("completions") or 1)
    generation = int(item.get("metadata", {}).get("generation") or 0)
    observed = int(status.get("observedGeneration") or 0)
    if observed < generation:
        return False
    if kind == "DaemonSet":
        desired = int(status.get("desiredNumberScheduled") or 0)
        return int(status.get("numberReady") or 0) >= desired
    desired = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
    return int(status.get("readyReplicas") or 0) >= desired
