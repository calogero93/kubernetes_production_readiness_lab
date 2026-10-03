"""A ready replica that already existed is not a replacement Pod."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from kubeproof.core.domain import Observation, SourceClass
from kubeproof.core.profile import CompanyProfile
from kubeproof.execution.runtime_checks import measure_recovery


def test_recovery_requires_a_new_uid_even_when_another_replica_is_ready(
    strict_profile: CompanyProfile,
) -> None:
    def pod(name: str, uid: str) -> dict[str, Any]:
        return {
            "metadata": {"name": name, "uid": uid},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        }

    deleted: list[str] = []
    first, existing, replacement = (
        pod("old", "old-uid"),
        pod("existing", "existing-uid"),
        pod("new", "new-uid"),
    )
    cluster = SimpleNamespace(
        pods=lambda *_: [existing, replacement] if deleted else [first, existing],
        delete_pod=lambda name, _: deleted.append(name),
    )
    deployment = {
        "metadata": {"namespace": "product", "name": "api"},
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "api"}}},
    }
    observations: list[Observation] = []

    def observe(check_id: str, observation_type: str, summary: str, **kwargs: Any) -> Observation:
        observation = Observation(
            id="recovery",
            check_id=check_id,
            observation_type=observation_type,
            summary=summary,
            source_class=SourceClass.RUNTIME,
            **kwargs,
        )
        observations.append(observation)
        return observation

    checks: dict[str, Any] = {}
    measure_recovery(
        cluster,
        [deployment],
        strict_profile,
        1,
        observe,
        checks,
        [],
        SimpleNamespace(triggered=threading.Event(), infrastructure_error=False),
    )
    assert deleted == ["old"]
    assert observations[0].data["replacement_pod_uid"] == "new-uid"
