"""Best-effort local tool and platform fingerprinting."""

from __future__ import annotations

import platform
import subprocess
import sys

from kubeproof.domain import ToolFingerprint


def _tool_version(executable: str, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, *arguments], capture_output=True, check=False, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip()[:200] or None


def capture_tool_fingerprint(
    *, helm_executable: str | None, used_kind: bool
) -> ToolFingerprint:
    return ToolFingerprint(
        python_version=platform.python_version(),
        platform=f"{sys.platform}/{platform.machine()}/{platform.release()}",
        helm_version=_tool_version(helm_executable, "version", "--short")
        if helm_executable
        else None,
        kind_version=_tool_version("kind", "version") if used_kind else None,
    )
