"""Code-enforced local execution admission policy."""

from __future__ import annotations

import hashlib
import json

from kubeproof.core.domain import AdmissionDecision, AdmissionOutcome, HttpProbeOptions, Observation
from kubeproof.core.plans import EvaluationPlan

_HARD_STOP_RULES = {
    "security.privileged": "KP-SAFE-001",
    "security.host_network": "KP-SAFE-002",
    "security.host_pid": "KP-SAFE-003",
    "security.host_ipc": "KP-SAFE-004",
    "security.host_path": "KP-SAFE-005",
    "security.unmasked_proc": "KP-SAFE-006",
    "security.dangerous_capability": "KP-SAFE-007",
}

OVERRIDABLE_HOST_RULES = frozenset({"KP-SAFE-002", "KP-SAFE-003", "KP-SAFE-004", "KP-SAFE-005"})


def approval_scope_sha256(
    *,
    chart_sha256: str,
    rendered_manifest_sha256: str,
    profile_sha256: str,
    values_sha256: tuple[str, ...],
    set_values_sha256: tuple[str, ...],
    release_name: str,
    namespace: str,
    matched_rule_ids: tuple[str, ...],
    http_probe: HttpProbeOptions | None = None,
    test_plan: EvaluationPlan | None = None,
) -> str:
    """Bind one local approval to exact inputs and the disclosed host-access rules."""
    payload: dict[str, object] = {
        "environment": "local-kind",
        "chart_sha256": chart_sha256,
        "rendered_manifest_sha256": rendered_manifest_sha256,
        "profile_sha256": profile_sha256,
        "values_sha256": values_sha256,
        "set_values_sha256": set_values_sha256,
        "release_name": release_name,
        "namespace": namespace,
        "matched_rule_ids": matched_rule_ids,
    }
    if http_probe is not None:
        payload["http_probe"] = http_probe.model_dump(mode="json")
    if test_plan is not None:
        payload["test_plan_sha256"] = test_plan.digest()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def decide_local_admission(observations: tuple[Observation, ...]) -> AdmissionDecision:
    matched = tuple(
        sorted(
            {
                rule
                for observation in observations
                if (rule := _HARD_STOP_RULES.get(observation.observation_type)) is not None
            }
        )
    )
    if matched:
        return AdmissionDecision(
            outcome=AdmissionOutcome.STATIC_ONLY,
            matched_rule_ids=matched,
            explanation=(
                "Rendered workloads request host-dangerous features that local kind does not "
                "safely isolate; runtime execution stops unless an exact supported local "
                "approval is recorded."
            ),
        )
    return AdmissionDecision(
        outcome=AdmissionOutcome.ADMIT,
        explanation=(
            "No v0.1 hard-stop feature was found. This permits only known, intentionally "
            "selected products and is not hostile-code containment."
        ),
    )
