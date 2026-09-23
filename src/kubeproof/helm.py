"""Bounded Helm rendering through an argument-vector subprocess adapter."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class HelmRenderError(RuntimeError):
    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


@dataclass(frozen=True)
class HelmRenderRequest:
    chart: str
    release_name: str = "kubeproof-target"
    namespace: str = "kubeproof-product"
    version: str | None = None
    values_files: tuple[Path, ...] = ()
    set_values: tuple[str, ...] = ()
    kubernetes_version: str | None = None
    timeout_seconds: int = 90


@dataclass(frozen=True)
class HelmRenderResult:
    manifest: bytes
    stderr: str


class HelmRenderer:
    def __init__(self, executable: str = "helm", max_output_bytes: int = 20 * 1024 * 1024) -> None:
        self._executable = executable
        self._max_output_bytes = max_output_bytes

    def render(self, request: HelmRenderRequest) -> HelmRenderResult:
        command = [
            self._executable,
            "template",
            request.release_name,
            request.chart,
            "--namespace",
            request.namespace,
            "--include-crds",
        ]
        if request.version:
            command.extend(("--version", request.version))
        if request.kubernetes_version:
            command.extend(("--kube-version", request.kubernetes_version))
        for values_file in request.values_files:
            command.extend(("--values", str(values_file.resolve())))
        for assignment in request.set_values:
            command.extend(("--set", assignment))

        with tempfile.TemporaryDirectory(prefix="kubeproof-helm-") as temp_dir:
            temp_path = Path(temp_dir)
            environment = os.environ.copy()
            environment.update(
                {
                    "HELM_CONFIG_HOME": str(temp_path / "config"),
                    "HELM_CACHE_HOME": str(temp_path / "cache"),
                    "HELM_DATA_HOME": str(temp_path / "data"),
                    "HELM_PLUGINS": str(temp_path / "plugins-disabled"),
                }
            )
            try:
                process = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    timeout=request.timeout_seconds,
                    env=environment,
                )
            except FileNotFoundError as exc:
                raise HelmRenderError(f"Helm executable not found: {self._executable}") from exc
            except subprocess.TimeoutExpired as exc:
                raise HelmRenderError(
                    f"Helm render exceeded {request.timeout_seconds} seconds"
                ) from exc

        stderr = process.stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise HelmRenderError(
                f"Helm render failed with exit code {process.returncode}", stderr=stderr
            )
        if len(process.stdout) > self._max_output_bytes:
            raise HelmRenderError(
                f"rendered manifest exceeds {self._max_output_bytes} byte safety limit"
            )
        return HelmRenderResult(manifest=process.stdout, stderr=stderr)


class HelmInstaller:
    """Install and remove only the named release in the disposable cluster."""

    def __init__(self, executable: str = "helm") -> None:
        self.executable = executable

    def _run(
        self,
        arguments: list[str],
        kubeconfig: Path,
        timeout: int,
        cancel_if: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory(prefix="kubeproof-helm-") as temp_dir:
            root = Path(temp_dir)
            environment = os.environ.copy()
            environment.update(
                {
                    "KUBECONFIG": str(kubeconfig),
                    "HELM_CONFIG_HOME": str(root / "config"),
                    "HELM_CACHE_HOME": str(root / "cache"),
                    "HELM_DATA_HOME": str(root / "data"),
                    "HELM_PLUGINS": str(root / "plugins-disabled"),
                }
            )
            try:
                command = [self.executable, *arguments]
                if cancel_if is None:
                    return subprocess.run(
                        command,
                        capture_output=True,
                        check=False,
                        timeout=timeout,
                        env=environment,
                    )
                with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                    process = subprocess.Popen(
                        command, stdout=stdout, stderr=stderr, env=environment
                    )
                    deadline = time.monotonic() + timeout
                    while process.poll() is None:
                        if cancel_if():
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                            raise HelmRenderError(
                                "Helm install cancelled by runtime safety monitor"
                            )
                        if time.monotonic() >= deadline:
                            process.kill()
                            process.wait()
                            raise HelmRenderError(f"Helm operation exceeded {timeout} seconds")
                        time.sleep(0.5)
                    stdout.seek(0)
                    stderr.seek(0)
                    return subprocess.CompletedProcess(
                        command, process.returncode, stdout.read(1_000_000), stderr.read(1_000_000)
                    )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise HelmRenderError(f"Helm operation failed: {exc}") from exc

    def install(
        self,
        request: HelmRenderRequest,
        kubeconfig: Path,
        timeout: int,
        cancel_if: Callable[[], bool] | None = None,
    ) -> None:
        args = [
            "install",
            request.release_name,
            request.chart,
            "--namespace",
            request.namespace,
            "--create-namespace",
            "--timeout",
            f"{timeout}s",
            "--wait",
            "--wait-for-jobs",
        ]
        if request.version:
            args.extend(("--version", request.version))
        for values_file in request.values_files:
            args.extend(("--values", str(values_file.resolve())))
        for assignment in request.set_values:
            args.extend(("--set", assignment))
        result = self._run(args, kubeconfig, timeout + 15, cancel_if=cancel_if)
        if result.returncode:
            raise HelmRenderError(
                f"Helm install failed ({result.returncode}): "
                + result.stderr.decode("utf-8", "replace")[-2000:]
            )

    def uninstall(self, release_name: str, namespace: str, kubeconfig: Path) -> None:
        result = self._run(
            ["uninstall", release_name, "--namespace", namespace, "--timeout", "60s"],
            kubeconfig,
            75,
        )
        if result.returncode:
            stderr = result.stderr.decode("utf-8", "replace")
            if "release: not found" in stderr:
                return
            raise HelmRenderError(f"Helm uninstall failed ({result.returncode}): " + stderr[-2000:])
