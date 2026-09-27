"""Evidence-producing resource, recovery and DNS checks for a ready product."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from kubeproof.core.domain import (
    Assessment,
    CheckResult,
    ExecutionStatus,
    Finding,
    Observation,
    ResourceRef,
    Severity,
)
from kubeproof.core.profile import CompanyProfile
from kubeproof.core.quantities import InvalidQuantity, parse_quantity
from kubeproof.execution.kubernetes import ClusterReader, KubernetesError
from kubeproof.execution.runtime_monitor import MetricsSnapshot, SafetyMonitor, UnsafeWorkloadError

RUNTIME_TITLES = {
    "runtime.installation": "Installation and readiness",
    "runtime.resources": "Observed resource consumption",
    "runtime.recovery": "Pod replacement readiness",
    "runtime.network": "Observed network and DNS behavior",
}


def untested(
    check_id: str,
    title: str,
    explanation: str,
    *,
    failed: bool = False,
    cancelled: bool = False,
) -> CheckResult:
    return CheckResult(
        id=check_id,
        title=title,
        execution_status=(
            ExecutionStatus.INFRASTRUCTURE_FAILURE
            if failed
            else ExecutionStatus.CANCELLED
            if cancelled
            else ExecutionStatus.COMPLETED
        ),
        assessment=Assessment.NOT_TESTED,
        explanation=explanation,
    )


def _selector(labels: dict[str, str]) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def measure_resources(
    snapshots: list[MetricsSnapshot],
    namespace: str,
    profile: CompanyProfile,
    origin: float,
    observe: Callable[..., Observation],
    checks: dict[str, CheckResult],
    findings: list[Finding],
) -> None:
    seen: set[tuple[str, str]] = set()
    samples: list[Observation] = []
    incomplete = False
    try:
        for snapshot in snapshots:
            live_pods = {str(item.get("metadata", {}).get("name")): item for item in snapshot.pods}
            for pod_metrics in snapshot.metrics:
                metadata = pod_metrics.get("metadata", {})
                pod_name = str(metadata.get("name", ""))
                pod = live_pods.get(pod_name)
                if pod is None:
                    incomplete = True
                    continue
                pod_uid = str(pod.get("metadata", {}).get("uid", ""))
                timestamp = str(pod_metrics.get("timestamp", ""))
                identity = (pod_uid, timestamp)
                if identity in seen:
                    continue
                seen.add(identity)
                expected = {
                    str(item.get("name")) for item in pod.get("spec", {}).get("containers", [])
                }
                containers = pod_metrics.get("containers", [])
                measured = {str(item.get("name")) for item in containers}
                complete = (
                    bool(expected)
                    and expected <= measured
                    and all(
                        key in item.get("usage", {})
                        for item in containers
                        for key in ("cpu", "memory")
                    )
                )
                incomplete = incomplete or not complete
                totals = {
                    key: sum(
                        (
                            parse_quantity(str(item.get("usage", {}).get(key, "0")))
                            for item in containers
                        ),
                        start=0,
                    )
                    for key in ("cpu", "memory")
                }
                sample = observe(
                    "runtime.resources",
                    "resources.pod_metrics_sample",
                    "Kubernetes Metrics API returned a Pod CPU/memory sample.",
                    resource=ResourceRef(
                        api_version="v1", kind="Pod", namespace=namespace, name=pod_name
                    ),
                    data={
                        "pod_uid": pod_uid,
                        "timestamp": timestamp,
                        "window": pod_metrics.get("window"),
                        "collection_elapsed_seconds": round(snapshot.captured_at - origin, 3),
                        "sampling_interval_seconds": 15,
                        "containers_expected": sorted(expected),
                        "containers_measured": sorted(measured),
                        "complete": complete,
                        "cpu_base_units": str(totals["cpu"]),
                        "memory_base_units": str(totals["memory"]),
                    },
                )
                samples.append(sample)
                if complete:
                    for key, threshold in (
                        ("cpu", profile.constraints.resources.max_sampled_cpu_per_pod),
                        ("memory", profile.constraints.resources.max_sampled_memory_per_pod),
                    ):
                        if threshold and totals[key] > parse_quantity(threshold):
                            findings.append(
                                Finding(
                                    id=f"runtime-finding-{len(findings) + 1:05d}",
                                    check_id="runtime.resources",
                                    severity=Severity.BLOCKER,
                                    title=f"Observed Pod {key} sample exceeds profile threshold",
                                    description=f"A complete Pod sample exceeded {threshold}.",
                                    observation_ids=(sample.id,),
                                    constraint=f"constraints.resources.max_sampled_{key}_per_pod",
                                )
                            )
    except InvalidQuantity as exc:
        checks["runtime.resources"] = untested(
            "runtime.resources", RUNTIME_TITLES["runtime.resources"], str(exc), failed=True
        )
        return
    checks["runtime.resources"] = CheckResult(
        id="runtime.resources",
        title=RUNTIME_TITLES["runtime.resources"],
        execution_status=ExecutionStatus.COMPLETED,
        assessment=Assessment.FAIL
        if any(item.check_id == "runtime.resources" for item in findings)
        else (Assessment.INCONCLUSIVE if incomplete or not samples else Assessment.PASS),
        observation_ids=tuple(item.id for item in samples),
        finding_ids=tuple(item.id for item in findings if item.check_id == "runtime.resources"),
        explanation=(
            "No complete product Pod sample was collected." if incomplete or not samples else None
        ),
    )


def measure_recovery(
    cluster: ClusterReader,
    deployments: list[dict[str, Any]],
    profile: CompanyProfile,
    maximum: int,
    observe: Callable[..., Observation],
    checks: dict[str, CheckResult],
    findings: list[Finding],
    monitor: SafetyMonitor,
) -> None:
    samples: list[Observation] = []
    limit = profile.constraints.reliability.max_pod_replacement_ready_seconds or 60
    try:
        for deployment in deployments[:maximum]:
            if monitor.triggered.is_set():
                if monitor.infrastructure_error:
                    raise KubernetesError(monitor.reason or "dynamic safety monitoring failed")
                raise UnsafeWorkloadError(
                    monitor.reason or "dynamic Pod violated local safety policy"
                )
            metadata = deployment.get("metadata", {})
            spec = deployment.get("spec", {})
            namespace = str(metadata.get("namespace", "default"))
            name = str(metadata.get("name", ""))
            selector = _selector(
                spec.get("selector", {}).get("match_labels", {})
                or spec.get("selector", {}).get("matchLabels", {})
            )
            if not selector:
                continue
            pods = cluster.pods(namespace, selector)
            ready_pods = [
                pod
                for pod in pods
                if pod.get("status", {}).get("phase") == "Running"
                and any(
                    condition.get("status") == "True"
                    for condition in pod.get("status", {}).get("conditions", [])
                    if condition.get("type") == "Ready"
                )
            ]
            desired = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
            if desired < 1 or len(ready_pods) < desired:
                continue
            old_name = str(ready_pods[0]["metadata"]["name"])
            old_uid = str(ready_pods[0]["metadata"]["uid"])
            started = time.monotonic()
            cluster.delete_pod(old_name, namespace)
            replacement: dict[str, Any] | None = None
            while time.monotonic() - started < limit + 30:
                if monitor.triggered.is_set():
                    if monitor.infrastructure_error:
                        raise KubernetesError(monitor.reason or "dynamic safety monitoring failed")
                    raise UnsafeWorkloadError(
                        monitor.reason or "dynamic Pod violated local safety policy"
                    )
                for pod in cluster.pods(namespace, selector):
                    status = pod.get("status", {})
                    if pod.get("metadata", {}).get("uid") == old_uid:
                        continue
                    if status.get("phase") == "Running" and any(
                        item.get("type") == "Ready" and item.get("status") == "True"
                        for item in status.get("conditions", [])
                    ):
                        replacement = pod
                        break
                if replacement:
                    break
                time.sleep(2)
            elapsed = round(time.monotonic() - started, 3)
            sample = observe(
                "runtime.recovery",
                "recovery.pod_replacement",
                "Deployment controller Pod replacement readiness was measured.",
                resource=ResourceRef(
                    api_version="apps/v1", kind="Deployment", namespace=namespace, name=name
                ),
                data={
                    "deleted_pod_uid": old_uid,
                    "replacement_pod_uid": replacement.get("metadata", {}).get("uid")
                    if replacement
                    else None,
                    "ready_seconds": elapsed if replacement else None,
                    "deadline_seconds": limit,
                },
            )
            samples.append(sample)
            if not replacement or elapsed > limit:
                configured = profile.constraints.reliability.max_pod_replacement_ready_seconds
                findings.append(
                    Finding(
                        id=f"runtime-finding-{len(findings) + 1:05d}",
                        check_id="runtime.recovery",
                        severity=Severity.BLOCKER if configured else Severity.WARNING,
                        title=(
                            "Pod replacement readiness exceeded profile threshold"
                            if configured
                            else "Pod replacement readiness exceeded observation deadline"
                        ),
                        description=(
                            f"Deployment {namespace}/{name} did not replace a Ready Pod "
                            f"within {limit} seconds."
                        ),
                        observation_ids=(sample.id,),
                        constraint=(
                            "constraints.reliability.max_pod_replacement_ready_seconds"
                            if configured
                            else None
                        ),
                    )
                )
    except KubernetesError as exc:
        checks["runtime.recovery"] = untested(
            "runtime.recovery", RUNTIME_TITLES["runtime.recovery"], str(exc), failed=True
        )
        return
    own_findings = [item for item in findings if item.check_id == "runtime.recovery"]
    checks["runtime.recovery"] = CheckResult(
        id="runtime.recovery",
        title=RUNTIME_TITLES["runtime.recovery"],
        execution_status=ExecutionStatus.COMPLETED,
        assessment=(
            Assessment.FAIL
            if any(item.severity is Severity.BLOCKER for item in own_findings)
            else Assessment.WARNING
            if own_findings
            else Assessment.NOT_APPLICABLE
            if not samples
            else Assessment.PASS
        ),
        observation_ids=tuple(item.id for item in samples),
        finding_ids=tuple(item.id for item in own_findings),
        explanation="No eligible Ready Deployment Pod was found." if not samples else None,
    )


_DNS_LOG = re.compile(
    r'(?P<ip>(?:\d{1,3}\.){3}\d{1,3}):\d+\s+-\s+\d+\s+"'
    r"(?:A|AAAA|CNAME|TXT|MX|SRV)\s+IN\s+(?P<domain>[a-zA-Z0-9_.-]+)"
)


def observe_dns(
    cluster: ClusterReader,
    namespace: str,
    profile: CompanyProfile,
    observe: Callable[..., Observation],
    checks: dict[str, CheckResult],
    findings: list[Finding],
    artifacts: dict[str, str],
) -> None:
    try:
        logs = cluster.coredns_logs()
        artifacts["artifacts/network/coredns.log"] = logs[-500_000:]
        pod_ips = cluster.pod_ip_map()
    except KubernetesError as exc:
        checks["runtime.network"] = untested(
            "runtime.network", RUNTIME_TITLES["runtime.network"], str(exc), failed=True
        )
        return
    observations: list[Observation] = []
    seen: set[tuple[str, str]] = set()
    for line in logs.splitlines():
        match = _DNS_LOG.search(line)
        if not match:
            continue
        pod = pod_ips.get(match.group("ip"))
        domain = match.group("domain").rstrip(".").lower()
        if pod is None or pod[0] != namespace or (pod[2], domain) in seen:
            continue
        if domain == "localhost" or domain.endswith(
            (".localhost", ".local", ".svc", ".svc.cluster.local", ".cluster.local")
        ):
            continue
        seen.add((pod[2], domain))
        observation = observe(
            "runtime.network",
            "network.external_dns_query",
            f"Product Pod queried {domain} through CoreDNS.",
            resource=ResourceRef(api_version="v1", kind="Pod", namespace=pod[0], name=pod[1]),
            data={"pod_uid": pod[2], "domain": domain},
        )
        observations.append(observation)
        networking = profile.constraints.networking
        allowed = networking.allowed_external_domains or ()
        if networking.allow_public_egress is False and not any(
            domain == entry or domain.endswith(f".{entry}") for entry in allowed
        ):
            findings.append(
                Finding(
                    id=f"runtime-finding-{len(findings) + 1:05d}",
                    check_id="runtime.network",
                    severity=Severity.WARNING,
                    title="Observed external DNS query may conflict with no-public-egress policy",
                    description=(f"Product Pod queried {domain}; a dependency was not proven."),
                    observation_ids=(observation.id,),
                    constraint="constraints.networking.allow_public_egress",
                    limitation="A block/recovery experiment is required to prove dependency.",
                )
            )
    has_findings = any(item.check_id == "runtime.network" for item in findings)
    checks["runtime.network"] = CheckResult(
        id="runtime.network",
        title=RUNTIME_TITLES["runtime.network"],
        execution_status=ExecutionStatus.COMPLETED,
        assessment=(
            Assessment.WARNING
            if has_findings
            else Assessment.PASS
            if observations
            else Assessment.INCONCLUSIVE
        ),
        observation_ids=tuple(item.id for item in observations),
        finding_ids=tuple(item.id for item in findings if item.check_id == "runtime.network"),
        explanation=(
            "No product external DNS query was observed in retained CoreDNS logs."
            if not observations
            else None
        ),
    )
