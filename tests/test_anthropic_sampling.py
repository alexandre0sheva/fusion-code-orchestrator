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
