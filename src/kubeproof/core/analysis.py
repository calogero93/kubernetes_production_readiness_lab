"""Deterministic static analysis of rendered Kubernetes resources."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from kubeproof.core.domain import (
    Assessment,
    CheckResult,
    ExecutionStatus,
    Finding,
    Observation,
    ResourceRef,
    Severity,
    SourceClass,
)
from kubeproof.core.manifests import iter_containers, iter_workloads, resource_ref
from kubeproof.core.profile import CompanyProfile
from kubeproof.core.quantities import InvalidQuantity, parse_quantity


@dataclass(frozen=True)
class AnalysisOutput:
    observations: tuple[Observation, ...]
    findings: tuple[Finding, ...]
    checks: tuple[CheckResult, ...]


@dataclass
class _Accumulator:
    observations: list[Observation] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def observe(
        self,
        check_id: str,
        observation_type: str,
        summary: str,
        *,
        resource: ResourceRef | None = None,
        data: dict[str, Any] | None = None,
    ) -> Observation:
        observation = Observation(
            id=f"obs-{len(self.observations) + 1:05d}",
            check_id=check_id,
            source_class=SourceClass.STATIC_INPUT,
            observation_type=observation_type,
            summary=summary,
            resource=resource,
            data=data or {},
        )
        self.observations.append(observation)
        return observation

    def finding(
        self,
        check_id: str,
        severity: Severity,
        title: str,
        description: str,
        observation: Observation,
        *,
        constraint: str | None = None,
        remediation: str | None = None,
        limitation: str | None = None,
    ) -> None:
        self.findings.append(
            Finding(
                id=f"finding-{len(self.findings) + 1:05d}",
                check_id=check_id,
                severity=severity,
                title=title,
                description=description,
                observation_ids=(observation.id,),
                constraint=constraint,
                remediation=remediation,
                limitation=limitation,
            )
        )


def _severity(violates_profile: bool) -> Severity:
    return Severity.BLOCKER if violates_profile else Severity.WARNING


def _container_ref(workload_ref: ResourceRef, container_name: str) -> ResourceRef:
    return workload_ref.model_copy(update={"container": container_name})


def _analyze_rbac(
    resources: tuple[dict[str, Any], ...], profile: CompanyProfile, result: _Accumulator
) -> None:
    security = profile.constraints.security
    for resource in resources:
        kind = resource.get("kind")
        ref = resource_ref(resource)
        if kind in {"RoleBinding", "ClusterRoleBinding"}:
            role_ref = resource.get("roleRef", {})
            if isinstance(role_ref, dict) and role_ref.get("name") == "cluster-admin":
                observation = result.observe(
                    "static.security",
                    "rbac.cluster_admin_binding",
                    "Binding references the built-in cluster-admin role.",
                    resource=ref,
                    data={"role_ref": role_ref, "subjects": resource.get("subjects", [])},
                )
                result.finding(
                    "static.security",
                    _severity(security.allow_cluster_admin is False),
                    "Cluster-admin binding",
                    "The rendered binding grants the built-in cluster-admin role.",
                    observation,
                    constraint=(
                        "constraints.security.allow_cluster_admin"
                        if security.allow_cluster_admin is False
                        else None
                    ),
                    remediation="Grant only the permissions required by the product components.",
                )
        if kind not in {"Role", "ClusterRole"}:
            continue
        rules = resource.get("rules", [])
        if not isinstance(rules, list):
            continue
        wildcard_fields: set[str] = set()
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            for field_name in ("verbs", "apiGroups", "resources", "nonResourceURLs"):
                values = rule.get(field_name, [])
                if isinstance(values, list) and "*" in values:
                    wildcard_fields.add(field_name)
        if wildcard_fields:
            observation = result.observe(
                "static.security",
                "rbac.wildcard_rule",
                "RBAC role contains wildcard permissions.",
                resource=ref,
                data={"wildcard_fields": sorted(wildcard_fields)},
            )
            result.finding(
                "static.security",
                _severity(security.allow_wildcard_rbac is False),
                "Wildcard RBAC permissions",
                "One or more rendered RBAC rules use wildcard permissions.",
                observation,
                constraint=(
                    "constraints.security.allow_wildcard_rbac"
                    if security.allow_wildcard_rbac is False
                    else None
                ),
                remediation="Replace wildcard entries with an explicit least-privilege rule set.",
            )


def _analyze_security(
    resources: tuple[dict[str, Any], ...], profile: CompanyProfile, result: _Accumulator
) -> None:
    constraints = profile.constraints.security
    dangerous_capabilities = {
        "SYS_ADMIN",
        "SYS_MODULE",
        "BPF",
        "PERFMON",
        "SYS_PTRACE",
        "NET_ADMIN",
        "NET_RAW",
    }
    for workload in iter_workloads(resources):
        pod_spec = workload.pod_spec
        for field_name, observation_type, profile_value, label, constraint_path in (
            (
                "hostNetwork",
                "security.host_network",
                constraints.allow_host_network,
                "host network",
                "constraints.security.allow_host_network",
            ),
            (
                "hostPID",
                "security.host_pid",
                constraints.allow_host_pid,
                "host PID namespace",
                "constraints.security.allow_host_pid",
            ),
            ("hostIPC", "security.host_ipc", None, "host IPC namespace", None),
        ):
            if pod_spec.get(field_name) is True:
                observation = result.observe(
                    "static.security",
                    observation_type,
                    f"Workload requests the {label}.",
                    resource=workload.ref,
                    data={field_name: True},
                )
                result.finding(
                    "static.security",
                    _severity(profile_value is False),
                    f"Workload requests {label}",
                    f"The rendered Pod specification sets {field_name}: true.",
                    observation,
                    constraint=constraint_path if profile_value is False else None,
                )
        volumes = pod_spec.get("volumes", [])
        if isinstance(volumes, list):
            for volume in volumes:
                if isinstance(volume, dict) and isinstance(volume.get("hostPath"), dict):
                    observation = result.observe(
                        "static.security",
                        "security.host_path",
                        "Workload declares a hostPath volume.",
                        resource=workload.ref,
                        data={
                            "volume": volume.get("name"),
                            "path": volume["hostPath"].get("path"),
                        },
                    )
                    result.finding(
                        "static.security",
                        _severity(constraints.allow_host_path is False),
                        "Host filesystem mount requested",
                        "The rendered Pod specification declares a hostPath volume.",
                        observation,
                        constraint=(
                            "constraints.security.allow_host_path"
                            if constraints.allow_host_path is False
                            else None
                        ),
                    )

        pod_context = pod_spec.get("securityContext", {})
        if not isinstance(pod_context, dict):
            pod_context = {}
        for _, container in iter_containers(workload):
            container_name = str(container["name"])
            ref = _container_ref(workload.ref, container_name)
            context = container.get("securityContext", {})
            if not isinstance(context, dict):
                context = {}
            if context.get("privileged") is True:
                observation = result.observe(
                    "static.security",
                    "security.privileged",
                    "Container requests privileged mode.",
                    resource=ref,
                    data={"privileged": True},
                )
                result.finding(
                    "static.security",
                    _severity(constraints.allow_privileged is False),
                    "Privileged container",
                    "The rendered container security context requests privileged mode.",
                    observation,
                    constraint=(
                        "constraints.security.allow_privileged"
                        if constraints.allow_privileged is False
                        else None
                    ),
                )

            effective_non_root = context.get("runAsNonRoot", pod_context.get("runAsNonRoot"))
            effective_uid = context.get("runAsUser", pod_context.get("runAsUser"))
            if effective_non_root is not True or effective_uid == 0:
                observation = result.observe(
                    "static.security",
                    "security.non_root_not_enforced",
                    "Non-root execution is not effectively enforced for the container.",
                    resource=ref,
                    data={
                        "effective_run_as_non_root": effective_non_root,
                        "effective_uid": effective_uid,
                    },
                )
                result.finding(
                    "static.security",
                    _severity(constraints.require_run_as_non_root is True),
                    "Non-root execution is not enforced",
                    (
                        "The effective Pod/container security context does not prove non-root "
                        "execution. This does not claim that the image actually runs as root."
                    ),
                    observation,
                    constraint=(
                        "constraints.security.require_run_as_non_root"
                        if constraints.require_run_as_non_root is True
                        else None
                    ),
                    remediation="Set runAsNonRoot: true and use a known non-zero runtime UID.",
                )

            if context.get("readOnlyRootFilesystem") is not True:
                observation = result.observe(
                    "static.security",
                    "security.writable_root_filesystem",
                    "Read-only root filesystem is not explicitly enabled.",
                    resource=ref,
                    data={"read_only_root_filesystem": context.get("readOnlyRootFilesystem")},
                )
                result.finding(
                    "static.security",
                    _severity(constraints.require_read_only_root_filesystem is True),
                    "Writable root filesystem is permitted",
                    "The container does not explicitly set readOnlyRootFilesystem: true.",
                    observation,
                    constraint=(
                        "constraints.security.require_read_only_root_filesystem"
                        if constraints.require_read_only_root_filesystem is True
                        else None
                    ),
                )

            if context.get("procMount") == "Unmasked":
                observation = result.observe(
                    "static.security",
                    "security.unmasked_proc",
                    "Container requests an unmasked proc filesystem.",
                    resource=ref,
                )
                result.finding(
                    "static.security",
                    Severity.WARNING,
                    "Unmasked proc filesystem",
                    "The container security context sets procMount to Unmasked.",
                    observation,
                )

            capabilities = context.get("capabilities", {})
            added = capabilities.get("add", []) if isinstance(capabilities, dict) else []
            if isinstance(added, list) and added:
                normalized = tuple(sorted(str(capability).upper() for capability in added))
                dangerous = tuple(cap for cap in normalized if cap in dangerous_capabilities)
                observation_type = (
                    "security.dangerous_capability" if dangerous else "security.added_capability"
                )
                observation = result.observe(
                    "static.security",
                    observation_type,
                    "Container adds Linux capabilities.",
                    resource=ref,
                    data={"added": normalized, "dangerous": dangerous},
                )
                allowed = constraints.allowed_added_capabilities
                disallowed = tuple(
                    cap for cap in normalized if allowed is not None and cap not in allowed
                )
                result.finding(
                    "static.security",
                    _severity(bool(disallowed)),
                    "Container adds Linux capabilities",
                    f"The rendered security context adds: {', '.join(normalized)}.",
                    observation,
                    constraint=(
                        "constraints.security.allowed_added_capabilities" if disallowed else None
                    ),
                )


def _resource_amount(container: dict[str, Any], section: str, name: str) -> Decimal | None:
    resources = container.get("resources", {})
    if not isinstance(resources, dict):
        return None
    values = resources.get(section, {})
    if not isinstance(values, dict) or name not in values:
        return None
    try:
        return parse_quantity(str(values[name]))
    except InvalidQuantity:
        return None


def _effective_pod_limit(workload: Any, resource_name: str) -> Decimal | None:
    regular: list[Decimal] = []
    initial: list[Decimal] = []
    for collection, container in iter_containers(workload):
        if collection == "ephemeralContainers":
            continue
        amount = _resource_amount(container, "limits", resource_name)
        if amount is None:
            return None
        if collection == "initContainers":
            initial.append(amount)
        elif collection == "containers":
            regular.append(amount)
    if not regular and not initial:
        return None
    return max(sum(regular, Decimal(0)), max(initial, default=Decimal(0)))


def _analyze_resources(
    resources: tuple[dict[str, Any], ...], profile: CompanyProfile, result: _Accumulator
) -> None:
    constraints = profile.constraints.resources
    for workload in iter_workloads(resources):
        for collection, container in iter_containers(workload):
            name = str(container["name"])
            declared = container.get("resources", {})
            if not isinstance(declared, dict):
                declared = {}
            requests = declared.get("requests", {})
            limits = declared.get("limits", {})
            requests = requests if isinstance(requests, dict) else {}
            limits = limits if isinstance(limits, dict) else {}
            observation = result.observe(
                "static.resources",
                "resources.container_configuration",
                "Container resource requests and limits were inspected.",
                resource=_container_ref(workload.ref, name),
                data={"collection": collection, "requests": requests, "limits": limits},
            )
            for resource_name in ("cpu", "memory"):
                if resource_name not in requests:
                    result.finding(
                        "static.resources",
                        _severity(constraints.require_requests is True),
                        f"{resource_name.capitalize()} request is missing",
                        f"The rendered container has no {resource_name} request.",
                        observation,
                        constraint=(
                            "constraints.resources.require_requests"
                            if constraints.require_requests is True
                            else None
                        ),
                    )
                if resource_name not in limits:
                    configured_maximum = (
                        constraints.max_cpu_limit_per_pod
                        if resource_name == "cpu"
                        else constraints.max_memory_limit_per_pod
                    )
                    violates_profile = (
                        constraints.require_limits is True or configured_maximum is not None
                    )
                    constraint = None
                    if configured_maximum is not None:
                        constraint = f"constraints.resources.max_{resource_name}_limit_per_pod"
                    elif constraints.require_limits is True:
                        constraint = "constraints.resources.require_limits"
                    result.finding(
                        "static.resources",
                        _severity(violates_profile),
                        f"{resource_name.capitalize()} limit is missing",
                        (
                            f"The rendered container has no {resource_name} limit; no finite "
                            "maximum is declared."
                        ),
                        observation,
                        constraint=constraint,
                    )

        for name, configured in (
            ("memory", constraints.max_memory_limit_per_pod),
            ("cpu", constraints.max_cpu_limit_per_pod),
        ):
            if configured is None:
                continue
            effective = _effective_pod_limit(workload, name)
            if effective is None:
                continue
            maximum_base_units = parse_quantity(configured)
            observation = result.observe(
                "static.resources",
                "resources.effective_pod_limit",
                f"Effective declared Pod {name} limit was calculated.",
                resource=workload.ref,
                data={
                    "resource": name,
                    "effective_base_units": str(effective),
                    "maximum_base_units": str(maximum_base_units),
                    "algorithm": "max(sum(regular containers), max(init containers))",
                },
            )
            if effective > maximum_base_units:
                result.finding(
                    "static.resources",
                    Severity.BLOCKER,
                    f"Declared Pod {name} limit exceeds profile maximum",
                    f"The effective declared limit exceeds the configured {configured} maximum.",
                    observation,
                    constraint=f"constraints.resources.max_{name}_limit_per_pod",
                )


def _analyze_availability(
    resources: tuple[dict[str, Any], ...], profile: CompanyProfile, result: _Accumulator
) -> None:
    minimum = profile.constraints.reliability.minimum_replicas
    for resource in resources:
        if resource.get("kind") != "Deployment":
            continue
        spec = resource.get("spec", {})
        replicas = spec.get("replicas", 1) if isinstance(spec, dict) else 1
        observation = result.observe(
            "static.availability",
            "availability.deployment_replicas",
            "Deployment replica count was inspected.",
            resource=resource_ref(resource),
            data={"replicas": replicas},
        )
        if minimum is not None and isinstance(replicas, int) and replicas < minimum:
            result.finding(
                "static.availability",
                Severity.BLOCKER,
                "Deployment replica count is below profile minimum",
                f"The Deployment declares {replicas} replicas; the profile requires {minimum}.",
                observation,
                constraint="constraints.reliability.minimum_replicas",
            )


_URL = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def _iter_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for nested in value.values():
            strings.extend(_iter_strings(nested))
        return strings
    if isinstance(value, list):
        strings = []
        for nested in value:
            strings.extend(_iter_strings(nested))
        return strings
    return []


def _is_external(host: str) -> bool:
    host = host.rstrip(".").lower()
    if host in {"localhost"} or host.endswith((".svc", ".svc.cluster.local", ".cluster.local")):
        return False
    try:
        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return "." in host


def _domain_allowed(domain: str, allowed: tuple[str, ...] | None) -> bool:
    return allowed is not None and any(
        domain == entry or domain.endswith(f".{entry}") for entry in allowed
    )


def _analyze_network(
    resources: tuple[dict[str, Any], ...], profile: CompanyProfile, result: _Accumulator
) -> None:
    constraints = profile.constraints.networking
    seen: set[tuple[str, str, str]] = set()
    for resource in resources:
        if resource.get("kind") == "Secret":
            continue
        ref = resource_ref(resource)
        for string in _iter_strings(resource):
            for url in _URL.findall(string):
                host = (urlparse(url).hostname or "").lower()
                key = (ref.kind, ref.name, host)
                if not host or not _is_external(host) or key in seen:
                    continue
                seen.add(key)
                observation = result.observe(
                    "static.network",
                    "network.external_url_in_manifest",
                    "Rendered configuration contains an external URL.",
                    resource=ref,
                    data={"domain": host, "url_scheme": urlparse(url).scheme},
                )
                if constraints.allow_public_egress is False and not _domain_allowed(
                    host, constraints.allowed_external_domains
                ):
                    result.finding(
                        "static.network",
                        Severity.WARNING,
                        "Potential external network dependency",
                        (
                            f"Rendered configuration references {host}, while the profile forbids "
                            "public egress. Static presence does not prove a runtime dependency."
                        ),
                        observation,
                        constraint="constraints.networking.allow_public_egress",
                        limitation=(
                            "Confirming a dependency requires a baseline/block/recovery runtime "
                            "experiment, which has not run."
                        ),
                    )


def _completed_check(
    check_id: str, title: str, observations: list[Observation], findings: list[Finding]
) -> CheckResult:
    own_observations = tuple(item.id for item in observations if item.check_id == check_id)
    own_findings = tuple(item for item in findings if item.check_id == check_id)
    if any(item.severity is Severity.BLOCKER for item in own_findings):
        assessment = Assessment.FAIL
    elif own_findings:
        assessment = Assessment.WARNING
    else:
        assessment = Assessment.PASS
    return CheckResult(
        id=check_id,
        title=title,
        execution_status=ExecutionStatus.COMPLETED,
        assessment=assessment,
        observation_ids=own_observations,
        finding_ids=tuple(item.id for item in own_findings),
    )


def analyze(resources: tuple[dict[str, Any], ...], profile: CompanyProfile) -> AnalysisOutput:
    result = _Accumulator()
    _analyze_rbac(resources, profile, result)
    _analyze_security(resources, profile, result)
    _analyze_resources(resources, profile, result)
    _analyze_availability(resources, profile, result)
    _analyze_network(resources, profile, result)

    workload_count = sum(1 for _ in iter_workloads(resources))
    for check_id, area in (
        ("static.security", "security and RBAC"),
        ("static.resources", "declared resources"),
        ("static.availability", "declared availability"),
        ("static.network", "declared endpoints"),
    ):
        result.observe(
            check_id,
            "analysis.rendered_scope",
            f"Rendered input was inspected for {area}.",
            data={
                "rendered_resource_count": len(resources),
                "rendered_workload_count": workload_count,
                "finding_count": sum(1 for item in result.findings if item.check_id == check_id),
            },
        )

    checks = [
        _completed_check(
            "static.security", "Static security and RBAC", result.observations, result.findings
        ),
        _completed_check(
            "static.resources", "Declared resources", result.observations, result.findings
        ),
        _completed_check(
            "static.availability", "Declared availability", result.observations, result.findings
        ),
        _completed_check(
            "static.network", "Declared external endpoints", result.observations, result.findings
        ),
    ]
    checks.extend(
        (
            CheckResult(
                id="runtime.installation",
                title="Installation and readiness",
                execution_status=ExecutionStatus.COMPLETED,
                assessment=Assessment.NOT_TESTED,
                explanation="The inspect command renders but does not install the chart.",
            ),
            CheckResult(
                id="runtime.resources",
                title="Observed resource consumption",
                execution_status=ExecutionStatus.COMPLETED,
                assessment=Assessment.NOT_TESTED,
                explanation="Metrics API collection is part of the next runtime slice.",
            ),
            CheckResult(
                id="runtime.recovery",
                title="Pod replacement readiness",
                execution_status=ExecutionStatus.COMPLETED,
                assessment=Assessment.NOT_TESTED,
                explanation="No pod termination experiment ran during static inspection.",
            ),
            CheckResult(
                id="runtime.network",
                title="Observed network and DNS behavior",
                execution_status=ExecutionStatus.COMPLETED,
                assessment=Assessment.NOT_TESTED,
                explanation="Static URLs are not runtime dependency evidence.",
            ),
        )
    )
    return AnalysisOutput(
        observations=tuple(result.observations),
        findings=tuple(result.findings),
        checks=tuple(checks),
    )
