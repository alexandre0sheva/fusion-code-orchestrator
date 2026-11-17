"""Routes: serve a vendor's models from its own API or through OpenRouter, per the catalog.

A ``RoutedProvider`` takes the vendor's place in the provider registry, so every caller still looks
providers up by ``entry.provider``. Per call it reads the model's ``route``: ``direct`` uses the
vendor's API, ``openrouter`` uses OpenRouter under ``openrouter_id``, ``auto`` uses the vendor's API
when its key is set and OpenRouter otherwise. The response's ``provider`` and ``model`` name the
route that served the call (``openrouter`` / ``anthropic/claude-haiku-5.5``), so the run record
says which one it was.
"""

from __future__ import annotations

from collections.abc import Mapping

from fusion.config.catalog import Catalog, ModelEntry
from fusion.config.env import PROVIDER_KEY_ENV, ROUTABLE_VENDORS
from fusion.providers.base import AuthError, ModelProvider, ModelRequest, ModelResponse


class RoutedProvider(ModelProvider):
    """One vendor's models, served directly or through OpenRouter."""

    def __init__(
        self,
        name: str,
        *,
        direct: ModelProvider | None,
        openrouter: ModelProvider | None,
        catalog: Catalog,
    ) -> None:
        self.name = name
        self.direct = direct
        self.openrouter = openrouter
        self._catalog = catalog

    def is_available(self) -> bool:
        return any(p is not None and p.is_available() for p in (self.direct, self.openrouter))

    def _direct_ready(self) -> bool:
        return self.direct is not None and self.direct.is_available()

    def _missing_key(self, model_id: str, entry: ModelEntry | None, why: str) -> AuthError:
        variable = PROVIDER_KEY_ENV.get(self.name, f"the {self.name} key")
        label = entry.alias if entry is not None and entry.alias else model_id
        return AuthError(f"{variable} not configured: {label} {why}")

    def route_for(self, model_id: str) -> tuple[ModelProvider, str]:
        """The provider that serves ``model_id`` and the ID to send it; raises ``AuthError``."""
        entry = self._catalog.find(self.name, model_id)
        route = entry.route if entry is not None else "direct"
        openrouter_id = entry.openrouter_id if entry is not None else None
        if route == "direct" or (route == "auto" and self._direct_ready()):
            if self.direct is not None and self._direct_ready():
                return self.direct, model_id
            if route == "direct" and entry is not None:
                raise self._missing_key(model_id, entry, "has route: direct")
        if openrouter_id and self.openrouter is not None and self.openrouter.is_available():
            return self.openrouter, openrouter_id
        if not openrouter_id:
            raise self._missing_key(model_id, entry, "has no openrouter_id to fall back to")
        raise self._missing_key(model_id, entry, "and OPENROUTER_API_KEY is not set either")

    async def complete(self, request: ModelRequest) -> ModelResponse:
        provider, model_id = self.route_for(request.model_id)
        if model_id != request.model_id:
            request = request.model_copy(update={"model_id": model_id})
        return await provider.complete(request)

    async def aclose(self) -> None:
        for provider in (self.direct, self.openrouter):
            if provider is not None:
                await provider.aclose()


def with_openrouter_routes(
    providers: Mapping[str, ModelProvider],
    openrouter: ModelProvider | None,
    catalog: Catalog,
) -> dict[str, ModelProvider]:
    """The registry with OpenRouter added and each routable vendor wrapped in a ``RoutedProvider``.

    Without an available OpenRouter provider the registry is returned unchanged.
    """
    result = dict(providers)
    if openrouter is None or not openrouter.is_available():
        return result
    result["openrouter"] = openrouter
    for vendor in ROUTABLE_VENDORS:
        result[vendor] = RoutedProvider(
            vendor, direct=providers.get(vendor), openrouter=openrouter, catalog=catalog
        )
    return result
