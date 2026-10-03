"""Pure rendering of existing evaluation facts."""

from __future__ import annotations

from collections import Counter

from kubeproof.core.domain import EvaluationResult, ResourceRef, Severity


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
    if evaluation.execution_options and (probe := evaluation.execution_options.http_probe):
        namespace = evaluation.execution_options.namespace
        p95_limit = probe.max_p95_ms if probe.max_p95_ms is not None else "unset"
        lines.extend(
            (
                "",
                "## HTTP Service probe",
                "",
                f"- Target: `http://{probe.service}.{namespace}.svc:{probe.port}{probe.path}`",
                f"- Sequential GET requests: `{probe.requests}`",
                f"- Expected HTTP status: `{probe.expected_status}`",
                f"- Maximum failed requests: `{probe.max_failed_requests}`",
                f"- Maximum p95 milliseconds: `{p95_limit}`",
            )
        )
        for observation in evaluation.observations:
            if observation.observation_type == "http.service_probe":
                measurement = observation.data["measurement"]
                lines.extend(
                    (
                        f"- Observed outcomes: `{measurement['outcomes']}`",
                        f"- Observed p95 milliseconds: `{measurement['p95_ms']}`",
                        f"- Evidence: `{observation.id}`",
                        f"- Limitations: {observation.data['limitation']}",
                    )
                )
    if plan := evaluation.execution_options.test_plan:
        lines.extend(
            (
                "",
                "## Frozen probe plan",
                "",
                f"- Origin: `{plan.origin}`",
                f"- Objective: {plan.objective}",
                f"- SHA-256: `{plan.digest()}`",
                f"- Catalog version: `{plan.catalog_version}`",
                f"- Budget: {plan.budget.max_tasks} tasks, "
                f"{plan.budget.max_elapsed_seconds} seconds, "
                f"{plan.budget.max_parallel_tasks} concurrent tasks.",
                "- The plan does not change during execution.",
                "",
            )
        )
        executions = (
            {item.task_id: item for item in evaluation.plan_execution.tasks}
            if evaluation.plan_execution
            else {}
        )
        for task in plan.tasks:
            run = executions.get(task.id)
            lines.extend(
                (
                    f"### {task.id} — {task.capability}",
                    "",
                    task.rationale,
                    f"- Dependencies: {', '.join(task.depends_on) or 'none'}; "
                    f"condition: `{task.when}`",
                    f"- Parameters: `{task.parameters.model_dump_json()}`",
                    f"- Execution: `{run.state if run else 'not_tested'}`",
                )
            )
            if run and run.explanation:
                lines.append(f"- Reason: {run.explanation}")
            for observation in evaluation.observations:
                if (
                    observation.data.get("plan_task_id") == task.id
                    and observation.observation_type == "http.service_probe"
                ):
                    measurement = observation.data["measurement"]
                    lines.extend(
                        (
                            f"- Observed outcomes: `{measurement['outcomes']}`",
                            f"- Observed p95 milliseconds: `{measurement['p95_ms']}`",
                            f"- Evidence: `{observation.id}`",
                            f"- Limitations: {observation.data['limitation']}",
                        )
                    )
            lines.append("")
    if interpretation := evaluation.ai_interpretation:
        lines.extend(
            (
                "",
                "## AI interpretation",
                "",
                f"Status: `{interpretation.status}`.",
                "This commentary does not change deterministic findings or apply manifest changes.",
                "",
            )
        )
        if interpretation.error:
            lines.append(f"Unavailable: {interpretation.error}")
        for point in interpretation.points:
            lines.extend(
                (
                    f"### {point.kind}",
                    "",
                    point.explanation,
                    "- Evidence: "
                    + ", ".join(f"`{key}`" for key in (*point.observation_ids, *point.check_ids)),
                )
            )
            if point.suggested_manifest_change:
                lines.append(f"- Suggested manifest change: {point.suggested_manifest_change}")
            if point.verification:
                lines.append(f"- Verify with a new evaluation: {point.verification}")
            lines.append("")
        lines.extend(f"- Limitation: {limit}" for limit in interpretation.limitations)
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
    if evaluation.admission.approval_scope_sha256:
        lines.append(
            "Local host-access approval scope SHA-256: "
            f"`{evaluation.admission.approval_scope_sha256}`"
        )
        lines.append("")
    if approval := evaluation.admission.operator_approval:
        lines.extend(
            (
                "Operator approval: self-declared local acknowledgement; "
                "identity was not authenticated.",
                f"- Operator label: {approval.operator_label}",
                f"- Reason: {approval.reason}",
                f"- Approved at: {approval.approved_at.isoformat()}",
                "- Approved host rules apply to product Pods observed during this run.",
                "",
            )
        )

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
        source_ref = f" ({observation.provenance.source_ref})" if observation.provenance else ""
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
