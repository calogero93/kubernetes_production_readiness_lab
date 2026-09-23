from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from kubeproof.execution.helm import HelmRenderer, HelmRenderRequest


def test_helm_adapter_uses_argument_vector_without_shell(monkeypatch: Any, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout=b"---\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    values = tmp_path / "values.yaml"
    values.write_text("replicaCount: 2\n")

    HelmRenderer().render(
        HelmRenderRequest(
            chart="oci://vendor/product",
            version="1.2.3",
            values_files=(values,),
            set_values=("feature.enabled=true",),
        )
    )

    assert captured["command"][:3] == ["helm", "template", "kubeproof-target"]
    assert captured["kwargs"].get("shell") is None
    assert "--version" in captured["command"]
    assert "--post-renderer" not in captured["command"]
