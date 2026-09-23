"""Pure rendering of existing evaluation facts."""

from __future__ import annotations

from collections import Counter

from kubeproof.domain import EvaluationResult, ResourceRef, Severity


def _resource_text(resource: ResourceRef | None) -> str:
    if resource is None:
        return "evaluation"
    namespace = f"{resource.namespace}/" if resource.namespace else ""
    container = f" (container {resource.container})" if resource.container else ""
    return f"{resource.kind} {namespace}{resource.name}{container}"


def render_markdown(evaluation: EvaluationResult) -> str:
    counts = Counter(finding.severity for finding in evaluation.findings)
    lines = [
        "# KubeProof evaluation",
        "",
        f"- Evaluation: `{evaluation.evaluation_id}`",
        f"- Schema: `{evaluation.schema_version}`",
        f"- Chart: `{evaluation.input.chart}`",
        f"- Requested version: `{evaluation.input.requested_version or 'unspecified'}`",
        f"- Resolved chart version: `{evaluation.input.resolved_version or 'unknown'}`",
        f"- Profile: `{evaluation.profile_name}`",
        f"- Local admission: `{evaluation.admission.outcome}`",
        f"- Adoption blockers: **{counts[Severity.BLOCKER]}**",
        f"- Warnings: **{counts[Severity.WARNING]}**",
        "",
        "## Check summary",
        "",
        "| Check | Execution | Assessment |",
        "|---|---|---|",
    ]
    for check in evaluation.checks:
        lines.append(f"| {check.title} | `{check.execution_status}` | `{check.assessment}` |")
    if evaluation.environment:
        lines.extend(
            (
                "",
                "## Runtime environment",
                "",
                f"- Provider: `{evaluation.environment.provider}`",
                f"- Cluster: `{evaluation.environment.cluster_name}`",
                f"- Cleanup attempted: `{evaluation.environment.cleanup_attempted}`",
                f"- Cleanup succeeded: `{evaluation.environment.cleanup_succeeded}`",
            )
        )
        if evaluation.environment.kubernetes_server_version:
            lines.append(
                f"- Kubernetes server: `{evaluation.environment.kubernetes_server_version}`"
            )
        if evaluation.environment.metrics_server_manifest_sha256:
            lines.append(
                "- Metrics Server manifest SHA-256: "
                f"`{evaluation.environment.metrics_server_manifest_sha256}`"
            )
        if evaluation.environment.cleanup_error:
            lines.append(f"- Cleanup error: {evaluation.environment.cleanup_error}")
        if evaluation.environment.leftovers_before_cluster_deletion:
            lines.append(
                "- Leftovers before cluster deletion: "
                + ", ".join(evaluation.environment.leftovers_before_cluster_deletion)
            )
        if evaluation.environment.runtime_safety_rule_ids:
            lines.append(
                "- Runtime safety stop rules: "
                + ", ".join(evaluation.environment.runtime_safety_rule_ids)
            )
        lines.append("")
    if evaluation.tool_fingerprint:
        lines.extend(
            (
                "",
                "## Tool fingerprint",
                "",
                f"- Python: `{evaluation.tool_fingerprint.python_version}`",
                f"- Platform: `{evaluation.tool_fingerprint.platform}`",
                f"- Helm: `{evaluation.tool_fingerprint.helm_version or 'unknown'}`",
                f"- kind: `{evaluation.tool_fingerprint.kind_version or 'not used or unknown'}`",
            )
        )
    lines.extend(("", "## Local safety admission", "", evaluation.admission.explanation, ""))
    if evaluation.admission.matched_rule_ids:
        lines.append(
            "Matched rules: "
            + ", ".join(f"`{rule}`" for rule in evaluation.admission.matched_rule_ids)
        )
        lines.append("")

    lines.extend(("## Findings", ""))
    if not evaluation.findings:
        lines.extend(("No deterministic findings were produced.", ""))
    for finding in evaluation.findings:
        evidence = ", ".join(f"`{item}`" for item in finding.observation_ids)
        lines.extend(
            (
                f"### [{finding.severity.upper()}] {finding.title}",
                "",
                finding.description,
                "",
                f"- Evidence: {evidence}",
                f"- Constraint: `{finding.constraint}`"
                if finding.constraint
                else "- Constraint: none",
            )
        )
        if finding.remediation:
            lines.append(f"- Possible remediation: {finding.remediation}")
        if finding.limitation:
            lines.append(f"- Limitation: {finding.limitation}")
        lines.append("")

    lines.extend(("## Observation index", ""))
    for observation in evaluation.observations:
        source_ref = (
            f" ({observation.provenance.source_ref})" if observation.provenance else ""
        )
        lines.append(
            f"- `{observation.id}` [{observation.source_class}] "
            f"{_resource_text(observation.resource)} — {observation.summary}{source_ref}"
        )
    lines.extend(("", "## Scope limitations", ""))
    if evaluation.environment is None:
        lines.append(
            "This bundle contains static rendered-input observations only. "
            "Runtime checks shown as `not_tested` did not run."
        )
    else:
        lines.append(
            "Runtime observations apply only to this disposable cluster and observation window."
        )
    lines.extend(
        (
            "A static URL or observed DNS query is not proof of a required network dependency. "
            "An absent security-context field is not proof of the image's runtime UID.",
            "",
        )
    )
    return "\n".join(lines)
