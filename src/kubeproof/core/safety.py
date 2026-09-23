"""Code-enforced local execution admission policy."""

from __future__ import annotations

from kubeproof.core.domain import AdmissionDecision, AdmissionOutcome, Observation

_HARD_STOP_RULES = {
    "security.privileged": "KP-SAFE-001",
    "security.host_network": "KP-SAFE-002",
    "security.host_pid": "KP-SAFE-003",
    "security.host_ipc": "KP-SAFE-004",
    "security.host_path": "KP-SAFE-005",
    "security.unmasked_proc": "KP-SAFE-006",
    "security.dangerous_capability": "KP-SAFE-007",
}


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
                "safely isolate; runtime execution is prohibited."
            ),
        )
    return AdmissionDecision(
        outcome=AdmissionOutcome.ADMIT,
        explanation=(
            "No v0.1 hard-stop feature was found. This permits only known, intentionally "
            "selected products and is not hostile-code containment."
        ),
    )
