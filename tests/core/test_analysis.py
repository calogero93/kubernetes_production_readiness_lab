from __future__ import annotations

from kubeproof.core.analysis import analyze
from kubeproof.core.domain import AdmissionOutcome, Severity
from kubeproof.core.manifests import parse_manifests
from kubeproof.core.profile import CompanyProfile
from kubeproof.core.safety import decide_local_admission


def test_static_analysis_produces_grounded_profile_blockers(
    strict_profile: CompanyProfile, unsafe_manifest: bytes
) -> None:
    analysis = analyze(parse_manifests(unsafe_manifest), strict_profile)
    observation_ids = {item.id for item in analysis.observations}

    assert analysis.findings
    assert all(check.observation_ids for check in analysis.checks if check.id.startswith("static."))
    assert all(finding.observation_ids for finding in analysis.findings)
    assert all(set(finding.observation_ids) <= observation_ids for finding in analysis.findings)
    assert any(
        finding.constraint == "constraints.security.allow_cluster_admin"
        for finding in analysis.findings
    )
    assert any(
        finding.constraint == "constraints.security.allow_wildcard_rbac"
        for finding in analysis.findings
    )
    assert any(finding.severity is Severity.BLOCKER for finding in analysis.findings)
    assert any(
        finding.title == "Potential external network dependency"
        and finding.severity is Severity.WARNING
        for finding in analysis.findings
    )


def test_host_dangerous_features_force_static_only(
    strict_profile: CompanyProfile, unsafe_manifest: bytes
) -> None:
    analysis = analyze(parse_manifests(unsafe_manifest), strict_profile)
    decision = decide_local_admission(analysis.observations)

    assert decision.outcome is AdmissionOutcome.STATIC_ONLY
    assert "KP-SAFE-001" in decision.matched_rule_ids
    assert "KP-SAFE-002" in decision.matched_rule_ids
    assert "KP-SAFE-005" in decision.matched_rule_ids
    assert "KP-SAFE-007" in decision.matched_rule_ids


def test_broad_rbac_is_visible_but_does_not_stop_local_admission(
    strict_profile: CompanyProfile,
) -> None:
    manifest = b"""\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: broad}
rules:
  - apiGroups: [\"*\"]
    resources: [\"*\"]
    verbs: [\"*\"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: admin}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects: []
"""
    analysis = analyze(parse_manifests(manifest), strict_profile)

    assert any(
        item.observation_type == "rbac.cluster_admin_binding" for item in analysis.observations
    )
    assert decide_local_admission(analysis.observations).outcome is AdmissionOutcome.ADMIT
