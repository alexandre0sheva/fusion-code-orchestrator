"""Model registry backed by YAML configuration."""

from __future__ import annotations

from fusion.config.loader import ModelEntry, ModelRegistryConfig, load_model_registry
from fusion.routing.budget import LOCAL_PROVIDERS, BudgetLevel, cost_tier_within_budget


class ModelRegistry:
    """Provides access to configured models and their metadata."""

    def __init__(self, config: ModelRegistryConfig | None = None) -> None:
        self._config = config or load_model_registry()

    @property
    def models(self) -> dict[str, ModelEntry]:
        return self._config.models

    def get(self, name: str) -> ModelEntry:
        if name not in self._config.models:
            msg = f"Unknown model: {name}"
            raise KeyError(msg)
        return self._config.models[name]

    def is_enabled(self, name: str) -> bool:
        return name in self._config.models and self._config.models[name].enabled

    def list_enabled(self) -> list[str]:
        return [name for name, entry in self._config.models.items() if entry.enabled]

    def list_by_strength(self, strength: str) -> list[str]:
        return [
            name
            for name, entry in self._config.models.items()
            if entry.enabled and strength in entry.strengths
        ]

    def list_by_provider(self, provider: str) -> list[str]:
        return [
            name
            for name, entry in self._config.models.items()
            if entry.enabled and entry.provider == provider
        ]

    def list_by_capability(self, capability: str) -> list[str]:
        """Backward-compatible capability lookup (strengths + legacy capabilities)."""
        return [
            name
            for name, entry in self._config.models.items()
            if entry.enabled
            and (capability in entry.strengths or capability in entry.capabilities)
        ]

    def filter_candidates(
        self,
        candidates: list[str],
        *,
        budget: BudgetLevel,
        require_json: bool = False,
        local_only: bool = False,
    ) -> list[str]:
        """Filter model aliases by enabled state, budget, and capabilities."""
        filtered: list[str] = []
        for alias in candidates:
            if alias not in self._config.models:
                continue
            entry = self._config.models[alias]
            if not entry.enabled:
                continue
            if local_only or budget == BudgetLevel.LOCAL_ONLY:
                if entry.provider not in LOCAL_PROVIDERS:
                    continue
            elif not cost_tier_within_budget(entry.cost_tier, budget):
                continue
            if require_json and not entry.supports_json:
                continue
            filtered.append(alias)
        return filtered
