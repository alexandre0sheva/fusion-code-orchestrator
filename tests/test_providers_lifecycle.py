"""Provider lifecycle (aclose) and per-provider rate-limit configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fusion.config.catalog import Catalog, ProviderLimits, load_catalog
from fusion.mcp_server import server as server_module
from fusion.mcp_server.tools import FusionTools
from fusion.orchestration.pipelines import build_provider_registry
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse, close_providers
from fusion.providers.limits import ProviderLimiter, build_limiters
from fusion.providers.mock import MockProvider


class SpyProvider(MockProvider):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(latency_ms=0.0)
        self.closed = 0
        self._fail = fail

    async def aclose(self) -> None:
        self.closed += 1
        if self._fail:
            raise RuntimeError("close failed")


async def test_close_providers_closes_each_and_survives_failures() -> None:
    good, bad, also_good = SpyProvider(), SpyProvider(fail=True), SpyProvider()
    providers: dict[str, ModelProvider] = {"a": good, "b": bad, "c": also_good}
    await close_providers(providers)
    assert (good.closed, bad.closed, also_good.closed) == (1, 1, 1)


async def test_default_aclose_is_a_noop() -> None:
    class Plain(ModelProvider):
        name = "plain"

        def is_available(self) -> bool:
            return True

        async def complete(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(provider=self.name, model=request.model_id)

    await Plain().aclose()


async def test_fusion_tools_aclose_closes_its_providers(tmp_path: Path) -> None:
    tools = FusionTools(db_path=str(tmp_path / "runs.db"), use_mock=True)
    spy = SpyProvider()
    tools.providers["mock"] = spy
    await tools.aclose()
    assert spy.closed == 1


async def test_mcp_server_closes_providers_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import Client

    created: list[FusionTools] = []

    class Capturing(FusionTools):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.providers["mock"] = SpyProvider()
            created.append(self)

    monkeypatch.setattr(server_module, "FusionTools", Capturing)
    server = server_module.create_mcp_server(db_path=str(tmp_path / "runs.db"))
    async with Client(server) as client:
        await client.list_tools()
    spy = created[0].providers["mock"]
    assert isinstance(spy, SpyProvider)
    assert spy.closed == 1


def test_packaged_catalog_declares_limits_for_cloud_providers() -> None:
    limits = load_catalog().provider_limits
    for provider in ("anthropic", "openai", "google"):
        assert limits[provider].max_concurrent is not None
        assert limits[provider].max_concurrent >= 1  # type: ignore[operator]


def test_provider_limits_reject_non_positive_values() -> None:
    with pytest.raises(ValidationError):
        ProviderLimits(max_concurrent=0)
    with pytest.raises(ValidationError):
        ProviderLimits(rpm=-5)


def test_build_limiters_follows_the_catalog() -> None:
    catalog = Catalog(
        models={}, provider_limits={"openai": ProviderLimits(max_concurrent=3, rpm=120)}
    )
    limiters = build_limiters(catalog)
    assert set(limiters) == {"openai"}
    assert limiters["openai"].max_concurrent == 3
    assert limiters["openai"].rpm == 120


def test_provider_registry_wires_catalog_limiters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    registry = build_provider_registry(use_mock=False)
    limiter = getattr(registry["anthropic"], "limiter", None)
    assert isinstance(limiter, ProviderLimiter)
    assert limiter.max_concurrent == load_catalog().provider_limits["anthropic"].max_concurrent
