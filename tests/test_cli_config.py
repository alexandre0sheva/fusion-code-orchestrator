"""CLI configuration validation tests."""

from __future__ import annotations

from typer.testing import CliRunner

from fusion.cli.app import app


def test_config_validate_warns_but_succeeds_without_keys() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0
    assert "Configuration valid" in result.output


def test_config_validate_strict_fails_without_keys() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["config", "validate", "--strict"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
