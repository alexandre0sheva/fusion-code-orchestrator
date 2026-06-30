"""Pytest configuration — keep tests offline with mock providers."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _force_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_DEFAULT_PROVIDER", "mock")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_ENABLED", raising=False)
    monkeypatch.delenv("LMSTUDIO_ENABLED", raising=False)
