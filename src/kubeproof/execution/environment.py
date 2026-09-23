"""Disposable kind environment with an isolated kubeconfig."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class EnvironmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class KindEnvironment:
    name: str
    kubeconfig: Path


class KindProvider:
    def __init__(self, executable: str = "kind") -> None:
        self.executable = executable

    def create(self, name: str, kubeconfig: Path, timeout: int = 240) -> KindEnvironment:
        command = [
            self.executable,
            "create",
            "cluster",
            "--name",
            name,
            "--kubeconfig",
            str(kubeconfig),
            "--wait",
            "120s",
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EnvironmentError(f"kind cluster creation failed: {exc}") from exc
        if result.returncode:
            message = result.stderr.decode("utf-8", "replace")[-2000:]
            raise EnvironmentError(f"kind cluster creation failed ({result.returncode}): {message}")
        return KindEnvironment(name=name, kubeconfig=kubeconfig)

    def delete(self, name: str, timeout: int = 120) -> None:
        try:
            result = subprocess.run(
                [self.executable, "delete", "cluster", "--name", name],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EnvironmentError(f"kind cluster deletion failed: {exc}") from exc
        if result.returncode:
            message = result.stderr.decode("utf-8", "replace")[-2000:]
            raise EnvironmentError(f"kind cluster deletion failed ({result.returncode}): {message}")


def temporary_kubeconfig() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    directory = tempfile.TemporaryDirectory(prefix="kubeproof-kind-")
    kubeconfig = Path(directory.name) / "config"
    os.chmod(directory.name, 0o700)
    return directory, kubeconfig
