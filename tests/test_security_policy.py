"""Security policy after the agent harness removal: logging controls only."""

from __future__ import annotations

import importlib

import pytest
from typer.testing import CliRunner

from fusion.cli.app import app
from fusion.security.policy import SecurityPolicy


def test_raw_prompt_logging_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUSION_LOG_RAW_PROMPTS", raising=False)
    policy = SecurityPolicy.from_env()
    assert policy.log_raw_prompts is False
    assert policy.sanitize_for_log("secret", "[REDACTED]") == "[REDACTED]"


def test_raw_prompt_logging_can_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_LOG_RAW_PROMPTS", "true")
    policy = SecurityPolicy.from_env()
    assert policy.sanitize_for_log("secret", "[REDACTED]") == "secret"


def test_agent_mode_env_no_longer_grants_any_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FUSION_AGENT_MODE", "true")
    policy = SecurityPolicy.from_env()
    assert not hasattr(policy, "allow_file_writes")
    assert not hasattr(policy, "allow_shell_execution")
    assert not hasattr(policy, "workspace_root")


def test_agent_harness_modules_are_gone() -> None:
    for module in ("fusion.agent", "fusion.benchmark.compare"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_compare_implement_cli_command_is_gone() -> None:
    result = CliRunner().invoke(app, ["compare-implement", "--help"])
    assert result.exit_code != 0
