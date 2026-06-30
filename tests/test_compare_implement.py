"""Tests for Opus vs Fusion compare benchmark."""

from __future__ import annotations

import pytest

from fusion.benchmark.compare import compare_implementations
from fusion.routing.budget import BudgetLevel


@pytest.mark.asyncio
async def test_compare_implement_mock(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_AGENT_MODE", "true")
    monkeypatch.setenv("FUSION_DEFAULT_PROVIDER", "mock")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")

    result = await compare_implementations(
        task="Add a note about caching to README",
        workspace_root=tmp_path,
        max_agent_steps=3,
        budget=BudgetLevel.LOW,
        opus_model="mock-fast",
        fusion_executor_model="mock-fast",
        use_mock=True,
    )

    assert result.opus.summary
    assert result.fusion.summary
    assert result.opus.usage.llm_calls >= 1
    assert result.fusion.usage.llm_calls >= 1
    assert result.fusion.orchestration_run_id
    assert result.cheaper_arm in {"opus", "fusion"}


@pytest.mark.asyncio
async def test_compare_requires_agent_mode(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUSION_AGENT_MODE", raising=False)
    monkeypatch.setenv("FUSION_DEFAULT_PROVIDER", "mock")

    result = await compare_implementations(
        task="noop",
        workspace_root=tmp_path,
        use_mock=True,
    )
    assert result.opus.error
    assert "FUSION_AGENT_MODE" in (result.opus.error or "")
