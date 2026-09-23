"""Compatibility entry point for existing editable KubeProof installations."""

from kubeproof.interfaces.cli import app

__all__ = ["app"]


if __name__ == "__main__":
    app()
