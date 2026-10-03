"""Exercise real Helm rendering, including optional production chart branches."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/charts/kubeproof"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="real Helm required")


def helm(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", *arguments], capture_output=True, text=True, check=check, timeout=30
    )


@pytest.mark.parametrize(
    "chart",
    [
        "deploy/charts/kubeproof",
        "examples/charts/demo",
        "examples/charts/cpu-fixture",
        "examples/charts/http-fixture",
    ],
)
def test_chart_passes_real_strict_lint(chart: str) -> None:
    helm("lint", "--strict", str(ROOT / chart))


@pytest.mark.parametrize("scenario", ["default", "ephemeral", "integrations"])
def test_production_chart_renders_consistent_workload_and_optional_resources(scenario: str) -> None:
    digest = "sha256:" + "a" * 64
    arguments = ["template", "ci-release", str(CHART), "--namespace", "ci-namespace"]
    if scenario == "ephemeral":
        arguments += ["--set", "persistence.enabled=false"]
    if scenario == "integrations":
        arguments += [
            "--set",
            f"image.digest={digest}",
            "--set",
            "model.provider=openai_compatible",
            "--set",
            "model.name=ci-model",
            "--set",
            "model.baseUrl=https://model.example/v1",
            "--set",
            "model.existingSecret=model-credentials",
            "--set",
            "langfuse.enabled=true",
            "--set",
            "langfuse.baseUrl=https://tracing.example",
            "--set",
            "langfuse.existingSecret=tracing-credentials",
            "--set",
            "monitoring.serviceMonitor.enabled=true",
            "--set",
            "persistence.storageClassName=ci-storage",
        ]
    rendered = helm(*arguments)
    documents: list[dict[str, Any]] = [item for item in yaml.safe_load_all(rendered.stdout) if item]
    resources = {item["kind"]: item for item in documents}
    deployment = resources["Deployment"]
    pod = deployment["spec"]["template"]
    spec = pod["spec"]
    container = spec["containers"][0]
    service = resources["Service"]

    assert service["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]
    assert service["spec"]["selector"].items() <= pod["metadata"]["labels"].items()
    assert service["spec"]["ports"][0]["targetPort"] == container["ports"][0]["name"]
    assert service["spec"]["type"] == "ClusterIP"
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["runAsUser"] == 10001
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    volumes = {item["name"]: item for item in spec["volumes"]}
    assert {item["name"] for item in container["volumeMounts"]} <= volumes.keys()
    if scenario == "ephemeral":
        assert "PersistentVolumeClaim" not in resources
        assert "emptyDir" in volumes["data"]
    else:
        assert (
            volumes["data"]["persistentVolumeClaim"]["claimName"]
            == resources["PersistentVolumeClaim"]["metadata"]["name"]
        )
    if scenario == "integrations":
        assert container["image"].endswith("@" + digest)
        assert resources["PersistentVolumeClaim"]["spec"]["storageClassName"] == "ci-storage"
        monitor = resources["ServiceMonitor"]["spec"]
        assert monitor["selector"]["matchLabels"].items() <= service["metadata"]["labels"].items()
        assert monitor["namespaceSelector"]["matchNames"] == ["ci-namespace"]
        assert monitor["endpoints"][0]["port"] == service["spec"]["ports"][0]["name"]
        env = {item["name"]: item for item in container["env"]}
        assert env["KUBEPROOF_AI_MODEL"]["value"] == "ci-model"
        assert env["KUBEPROOF_AI_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == (
            "model-credentials"
        )
        assert env["LANGFUSE_SECRET_KEY"]["valueFrom"]["secretKeyRef"]["name"] == (
            "tracing-credentials"
        )
    else:
        assert "ServiceMonitor" not in resources
        assert not any(item["name"] == "KUBEPROOF_AI_API_KEY" for item in container["env"])


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (["model.provider=openai_compatible"], "model.name is required"),
        (
            ["langfuse.enabled=true", "langfuse.baseUrl=https://tracing.example"],
            "langfuse.existingSecret is required",
        ),
    ],
)
def test_chart_rejects_incomplete_optional_integration(settings: list[str], message: str) -> None:
    arguments = ["template", "ci-release", str(CHART)]
    for setting in settings:
        arguments += ["--set", setting]
    result = helm(*arguments, check=False)
    assert result.returncode != 0
    assert message in result.stderr
