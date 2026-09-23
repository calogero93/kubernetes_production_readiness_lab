from __future__ import annotations

from typer.testing import CliRunner

from kubeproof.cli import app


def test_cli_exposes_inspect_as_explicit_subcommand() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "inspect" in result.stdout
    assert "verify" in result.stdout
    assert "compare" in result.stdout
    assert "history" in result.stdout
    assert "serve" in result.stdout
