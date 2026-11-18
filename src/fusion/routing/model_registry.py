"""Model registry backed by YAML configuration."""

from __future__ import annotations

from fusion.config.loader import ModelEntry, ModelRegistryConfig, load_model_registry


class ModelRegistry:
    """Provides access to configured models and their metadata."""

    def __init__(self, config: ModelRegistryConfig | None = None) -> None:
        self._config = config or load_model_registry()

    @classmethod
    def for_mode(
        cls, *, use_mock: bool, config: ModelRegistryConfig | None = None
    ) -> ModelRegistry:
        """A registry holding only mock models (offline mode) or only real models (live mode).

        Choosing the model set here, once, keeps routing and role fallbacks free of "am I in
        test mode?" branches: a live registry can never fall back to a mock model and vice versa.
        """
        base = config or load_model_registry()
        models = {
            alias: entry
            for alias, entry in base.models.items()
            if (entry.provider == "mock") == use_mock
        }
        return cls(ModelRegistryConfig(models=models))

    @property
    def models(self) -> dict[str, ModelEntry]:
        return self._config.models

    def get(self, name: str) -> ModelEntry:
        if name not in self._config.models:
            msg = f"Unknown model: {name}"
            raise KeyError(msg)
        return self._config.models[name]

    def add_seat(self, seat: str, base: str) -> None:
        """Register ``seat`` (``gpt-luna#2``) as another copy of ``base`` in one panel.

        The copy has no roles, so it is never picked by role or as a spare, and it keeps the
        base model's alias for pricing. Adding the same seat again changes nothing.
        """
        if seat == base or seat in self._config.models:
            return
        entry = self.get(base)
        number = seat.rsplit("#", 1)[-1]
        self._config.models[seat] = entry.model_copy(
            update={
                "alias": entry.alias or base,
                "display_name": f"{entry.display_name or base} (seat {number})",
                "roles": [],
            }
        )

    def is_enabled(self, name: str) -> bool:
        return name in self._config.models and self._config.models[name].enabled

    def list_enabled(self) -> list[str]:
        return [name for name, entry in self._config.models.items() if entry.enabled]

    def by_role(self, role: str) -> list[str]:
        """Enabled aliases that declare a catalog role (panel, judge, synthesizer, baseline)."""
        return [
            name
            for name, entry in self._config.models.items()
            if entry.enabled and role in entry.roles
        ]

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
            if entry.enabled and (capability in entry.strengths or capability in entry.capabilities)
        ]
