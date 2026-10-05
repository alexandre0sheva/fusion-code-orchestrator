"""Layered configuration with per-key provenance.

Precedence, lowest to highest: packaged defaults, user config, project config, environment
variables (``FUSION__SECTION__KEY=value``), then command-line ``--set`` overrides. Mappings are
merged key by key; lists and scalars are replaced. Every leaf remembers which layer set it so
``fusion config show --resolved`` and validation errors can point at the source.
"""

from __future__ import annotations

import copy
import difflib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from fusion.config import paths

_PACKAGED_DIR = Path(__file__).parent
# Packaged defaults are split over four files for readability; top-level keys are disjoint.
_PACKAGED_FILES = ("catalog.yaml", "routing_policies.yaml", "strategies.yaml", "baseline.yaml")
# Top-level sections users may set. Add new sections here when a new config area appears.
SECTIONS = frozenset(
    {
        "models",
        "provider_limits",
        "policies",
        "budgets",
        "fanout",
        "refinement",
        "cache",
        "strategies",
        "budget_strategies",
        "baselines",
    }
)
ENV_PREFIX = "FUSION__"
PACKAGED = "packaged"


class ConfigError(ValueError):
    """A configuration problem, phrased for the person who has to fix it."""


@dataclass(frozen=True)
class Layer:
    name: str
    data: dict[str, Any]
    path: Path | None = None


@dataclass
class ResolvedConfig:
    data: dict[str, Any]
    origins: dict[str, str]
    layers: list[Layer] = field(default_factory=list)

    def origin_of(self, dotted: str) -> str | None:
        """Origin of a key, falling back to the nearest recorded parent."""
        parts = dotted.split(".")
        while parts:
            key = ".".join(parts)
            if key in self.origins:
                return self.origins[key]
            parts.pop()
        return None


_cli_overrides: dict[str, Any] = {}


def set_cli_overrides(overrides: Mapping[str, Any]) -> None:
    """Install process-wide ``--set`` overrides (dotted key to value)."""
    _cli_overrides.clear()
    _cli_overrides.update(overrides)


def parse_scalar(text: str) -> Any:
    """Interpret an env/CLI string as YAML (``4``, ``true``, ``[high]``), else keep the text."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``base`` updated by ``override``; mappings merge, everything else is replaced."""
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _leaves(data: Mapping[str, Any], prefix: str = "") -> list[str]:
    paths_: list[str] = []
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            paths_.extend(_leaves(value, f"{dotted}."))
        else:
            paths_.append(dotted)
    return paths_


def _nested(dotted: str, value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    cursor = result
    parts = dotted.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        msg = f"{path}: invalid YAML{where}: {problem}"
        raise ConfigError(msg) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        msg = f"{path}: expected a mapping of settings at the top level, got {type(raw).__name__}"
        raise ConfigError(msg)
    return raw


def _check_sections(data: Mapping[str, Any], source: Path | str) -> None:
    for key in data:
        if key in SECTIONS:
            continue
        close = difflib.get_close_matches(str(key), sorted(SECTIONS), n=1)
        hint = f"; did you mean '{close[0]}'?" if close else f"; valid keys: {sorted(SECTIONS)}"
        msg = f"{source}: unknown setting '{key}'{hint}"
        raise ConfigError(msg)


@cache
def _packaged_cached() -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for name in _PACKAGED_FILES:
        merged.update(_read_yaml(_PACKAGED_DIR / name))
    return merged


def packaged_defaults() -> dict[str, Any]:
    return copy.deepcopy(_packaged_cached())


def _env_layer(env: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    data: dict[str, Any] = {}
    origin_by_key: dict[str, str] = {}
    for name in sorted(env):
        if not name.upper().startswith(ENV_PREFIX):
            continue
        parts = [p for p in name[len(ENV_PREFIX) :].lower().split("__") if p]
        if not parts:
            continue
        dotted = ".".join(parts)
        data = deep_merge(data, _nested(dotted, parse_scalar(env[name])))
        origin_by_key[dotted] = f"env:{name}"
    return data, origin_by_key


def resolve_config(
    *,
    env: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> ResolvedConfig:
    """Merge all layers. ``env`` and ``cli_overrides`` default to the process state."""
    environment = os.environ if env is None else env
    cli = dict(_cli_overrides if cli_overrides is None else cli_overrides)

    layers: list[Layer] = [Layer(PACKAGED, packaged_defaults())]
    for label, file in (
        ("user", paths.user_config_file()),
        ("project", paths.project_config_file()),
    ):
        if file.is_file():
            data = _read_yaml(file)
            _check_sections(data, file)
            layers.append(Layer(f"{label}:{file}", data, file))
    env_data, env_origins = _env_layer(environment)
    _check_sections(env_data, "environment variables")
    layers.append(Layer("env", env_data))
    cli_data: dict[str, Any] = {}
    for dotted, value in cli.items():
        cli_data = deep_merge(cli_data, _nested(dotted, value))
    _check_sections(cli_data, "--set flags")
    layers.append(Layer("cli", cli_data))

    merged: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for layer in layers:
        merged = deep_merge(merged, layer.data)
        for leaf in _leaves(layer.data):
            origins[leaf] = env_origins.get(leaf, "env") if layer.name == "env" else layer.name
    live = set(_leaves(merged))  # drop origins of children a later layer replaced wholesale
    origins = {leaf: origin for leaf, origin in origins.items() if leaf in live}
    return ResolvedConfig(merged, origins, layers)


def flatten(data: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Dotted leaf keys with their values, in document order."""
    rows: list[tuple[str, Any]] = []

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict) and node:
            for key, value in node.items():
                walk(value, f"{prefix}{key}.")
        else:
            rows.append((prefix.rstrip("."), node))

    walk(dict(data), "")
    return rows


def format_validation_error(
    exc: Exception, *, section_prefix: str, resolved: ResolvedConfig | None
) -> ConfigError:
    """Turn a pydantic ``ValidationError`` into key + origin + message lines."""
    errors = getattr(exc, "errors", None)
    if errors is None:
        return ConfigError(str(exc))
    lines = ["Invalid configuration:"]
    for err in errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        key = f"{section_prefix}.{loc}".strip(".") if loc else section_prefix
        origin = resolved.origin_of(key) if resolved else None
        where = f" (set by {_describe(origin)})" if origin else ""
        message = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
        lines.append(f"  - {key} = {err.get('input')!r}{where}: {message}")
    lines.append("Run `fusion config show --resolved` to see every value and where it came from.")
    return ConfigError("\n".join(lines))


def _describe(origin: str) -> str:
    if origin == PACKAGED:
        return "the packaged defaults"
    if origin == "cli":
        return "the --set flag"
    kind, _, rest = origin.partition(":")
    return {
        "user": "user config ",
        "project": "project config ",
        "env": "environment variable ",
    }.get(kind, "") + (rest or origin)
