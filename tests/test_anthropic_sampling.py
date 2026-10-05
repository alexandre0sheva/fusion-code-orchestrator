"""Tests for Anthropic sampling-parameter compatibility."""

from __future__ import annotations

from fusion.providers.anthropic import supports_sampling_params


def test_opus_48_rejects_sampling_params() -> None:
    assert supports_sampling_params("claude-opus-4-8") is False


def test_opus_47_rejects_sampling_params() -> None:
    assert supports_sampling_params("claude-opus-4-7") is False


def test_opus_46_allows_sampling_params() -> None:
    assert supports_sampling_params("claude-opus-4-6") is True


def test_sonnet_allows_sampling_params() -> None:
    assert supports_sampling_params("claude-sonnet-4-6") is True


def test_opus_5_and_sonnet_5_reject_sampling_params() -> None:
    assert supports_sampling_params("claude-opus-5-5") is False
    assert supports_sampling_params("claude-sonnet-5-5") is False
    assert supports_sampling_params("claude-fable-5-1") is False


def test_haiku_4_5_still_accepts_sampling_params() -> None:
    assert supports_sampling_params("claude-haiku-4-5-20251001") is True
    assert supports_sampling_params("claude-haiku-4-5") is True
