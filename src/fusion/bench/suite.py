"""Suites: the studies the roadmap's final report is made of, kept as data.

A suite is a YAML file of ``BenchConfig`` fields (dataset, split, a per-category task quota, arms
with overrides, repeats, budgets) plus a ``description`` and a ``stage``. ``fusion bench plan``
and ``run`` take ``--suite NAME`` for a packaged one or ``--suite PATH`` for your own; command
line options still win. Keeping a study in a file is what lets it be priced, reviewed and frozen
before any money is spent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from fusion.config.layers import ConfigError

__all__ = ["SUITES_DIR", "Suite", "list_suites", "load_suite"]

SUITES_DIR = Path(__file__).parent / "suites"
_METADATA = ("description", "stage")


class Suite(BaseModel):
    name: str
    description: str
    stage: str  # "tune on dev" or "final study on test"
    path: Path
    config: dict[str, Any] = Field(default_factory=dict)  # BenchConfig fields, as written

    @property
    def arms(self) -> list[str]:
        return [str(a["name"]) for a in self.config.get("arms", [])]


def _read(path: Path) -> Suite:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        msg = f"Cannot read suite {path}: {exc}"
        raise ConfigError(msg) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("arms"), list):
        msg = f"Suite {path} must be a mapping with an 'arms' list"
        raise ConfigError(msg)
    config = {k: v for k, v in raw.items() if k not in _METADATA}
    return Suite(
        name=path.stem,
        description=" ".join(str(raw.get("description", "")).split()),
        stage=str(raw.get("stage", "")),
        path=path,
        config=config,
    )


def list_suites() -> list[Suite]:
    """The packaged suites, by name."""
    return [_read(p) for p in sorted(SUITES_DIR.glob("*.yaml"))]


def load_suite(name_or_path: str | Path) -> Suite:
    """A packaged suite by name (``ablation``) or a suite file by path."""
    path = Path(name_or_path)
    if path.suffix in (".yaml", ".yml") and path.is_file():
        return _read(path)
    packaged = SUITES_DIR / f"{name_or_path}.yaml"
    if packaged.is_file():
        return _read(packaged)
    known = ", ".join(s.name for s in list_suites())
    msg = f"No suite '{name_or_path}' (packaged suites: {known}; or give the path of a YAML file)"
    raise ConfigError(msg)
