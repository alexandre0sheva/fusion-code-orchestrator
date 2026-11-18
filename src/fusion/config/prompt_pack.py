"""The prompt pack: the prompts `fusion tune` may edit, kept as Markdown files, not code.

Each prompt is one file in ``config/prompts`` (``<name>.md``); ``PROMPT_SPECS`` names them, says
which axis tuning edits them along (``inv``: procedure, such as output shape and verification
steps; ``dom``: coding knowledge per task type) and which ``{{placeholders}}`` the code fills in.
A placeholder a prompt needs (the untrusted-input rules, for one) must stay, so an edit cannot drop
a safety rule by accident.

A directory named by ``FUSION_PROMPT_PACK`` overrides the packaged files it contains, so a tuned
pack is adopted without touching the code. Code reads prompts through ``active_pack()``, which a
caller can switch for one run with ``use_prompt_pack`` (how `fusion tune` probes a candidate).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from fusion.config.layers import ConfigError

__all__ = [
    "PACK_DIR",
    "PACK_ENV",
    "PROMPT_SPECS",
    "Axis",
    "PromptPack",
    "PromptSpec",
    "active_pack",
    "default_pack",
    "load_prompt_pack",
    "packaged_pack",
    "pack_errors",
    "use_prompt_pack",
]

PACK_DIR = Path(__file__).parent / "prompts"
PACK_ENV = "FUSION_PROMPT_PACK"
Axis = Literal["inv", "dom"]
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class PromptSpec:
    """One prompt of the pack: its tuning axis, the placeholders it must keep, what it is for."""

    axis: Axis
    summary: str
    placeholders: frozenset[str] = frozenset()


PROMPT_SPECS: Mapping[str, PromptSpec] = MappingProxyType(
    {
        "rules": PromptSpec(
            "inv",
            "rules every system prompt ends with (grounding, uncertainty, JSON output)",
            frozenset({"untrusted_rules"}),
        ),
        "claims": PromptSpec("inv", "the panel's answer format: a summary and atomic claims"),
        "select": PromptSpec(
            "inv",
            "how the selector ranks whole candidate answers (criteria in order)",
            frozenset({"shape"}),
        ),
        "self_check": PromptSpec("inv", "rung 0's self-check before it answers alone"),
        "role.judge": PromptSpec("inv", "the judge's role"),
        "role.synthesizer": PromptSpec("inv", "the synthesizer's role"),
        "role.coding_reviewer": PromptSpec("dom", "code review (also the code_review system)"),
        "role.security_reviewer": PromptSpec("dom", "security review persona"),
        "role.performance_reviewer": PromptSpec("dom", "performance review persona"),
        "role.debugging_hypothesis": PromptSpec("dom", "debugging (also the debugging system)"),
        "role.architecture_advisor": PromptSpec("dom", "architecture decisions"),
        "role.implementation_planner": PromptSpec("dom", "implementation plans"),
        "task.default": PromptSpec("dom", "every other request (questions, coding answers)"),
    }
)


@dataclass(frozen=True)
class PromptPack:
    """Every prompt of ``PROMPT_SPECS`` by name. Immutable; edits make a new pack."""

    texts: Mapping[str, str]
    origin: str = "packaged"
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        frozen = MappingProxyType({name: self.texts[name] for name in sorted(self.texts)})
        object.__setattr__(self, "texts", frozen)
        blob = "\x00".join(f"{name}\x01{text}" for name, text in frozen.items())
        object.__setattr__(self, "digest", hashlib.sha256(blob.encode()).hexdigest()[:16])

    def text(self, name: str) -> str:
        return self.texts[name]

    def render(self, name: str, **values: str) -> str:
        """The prompt with its placeholders filled; a placeholder without a value is an error."""

        def fill(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                msg = f"prompt '{name}' needs a value for {{{{{key}}}}}"
                raise KeyError(msg)
            return values[key]

        return _PLACEHOLDER.sub(fill, self.texts[name])

    def with_edits(self, edits: Mapping[str, str], origin: str | None = None) -> PromptPack:
        """A new pack with ``edits`` (name → full new text) applied. Unknown names raise."""
        unknown = sorted(set(edits) - set(self.texts))
        if unknown:
            raise KeyError(f"not a prompt of the pack: {', '.join(unknown)}")
        texts = {**self.texts, **{name: text.strip("\n") for name, text in edits.items()}}
        return PromptPack(texts, origin or self.origin)

    def changed(self, other: PromptPack) -> list[str]:
        """Names whose text differs between this pack and ``other``."""
        return [n for n in self.texts if self.texts[n] != other.texts.get(n)]

    def diff(self, other: PromptPack) -> str:
        """A unified diff from this pack to ``other``, one file per changed prompt."""
        parts: list[str] = []
        for name in self.changed(other):
            parts.extend(
                difflib.unified_diff(
                    (self.texts[name] + "\n").splitlines(keepends=True),
                    (other.texts[name] + "\n").splitlines(keepends=True),
                    fromfile=f"a/{name}.md",
                    tofile=f"b/{name}.md",
                )
            )
        return "".join(parts)

    def write(self, directory: Path) -> None:
        """Write every prompt as ``<name>.md`` (a directory ``FUSION_PROMPT_PACK`` can name)."""
        directory.mkdir(parents=True, exist_ok=True)
        for name, text in self.texts.items():
            (directory / f"{name}.md").write_text(text + "\n", encoding="utf-8")


def pack_errors(pack: PromptPack) -> list[str]:
    """What is wrong with a pack: missing or unknown prompts, empty ones, and placeholders that
    the code would not fill or that a prompt dropped."""
    errors: list[str] = []
    for name in sorted(set(PROMPT_SPECS) - set(pack.texts)):
        errors.append(f"prompt '{name}' is missing")
    for name in sorted(set(pack.texts) - set(PROMPT_SPECS)):
        errors.append(f"'{name}' is not a prompt of the pack")
    for name, spec in PROMPT_SPECS.items():
        text = pack.texts.get(name)
        if text is None:
            continue
        if not text.strip():
            errors.append(f"prompt '{name}' is empty")
        used = set(_PLACEHOLDER.findall(text))
        errors.extend(
            f"prompt '{name}' uses an unknown placeholder {{{{{key}}}}}"
            for key in sorted(used - spec.placeholders)
        )
        errors.extend(
            f"prompt '{name}' must keep the placeholder {{{{{key}}}}}"
            for key in sorted(spec.placeholders - used)
        )
    return errors


def _read_dir(directory: Path) -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8").strip("\n")
        for path in sorted(directory.glob("*.md"))
    }


def load_prompt_pack(directory: Path | None = None) -> PromptPack:
    """The packaged prompts, with the files of ``directory`` (if given) in place of theirs.

    Raises ``ConfigError`` when the directory holds a file that is not a prompt of the pack or the
    result does not validate.
    """
    texts = _read_dir(PACK_DIR)
    origin = "packaged"
    if directory is not None:
        if not directory.is_dir():
            msg = f"The prompt pack directory {directory} does not exist"
            raise ConfigError(msg)
        override = _read_dir(directory)
        unknown = sorted(set(override) - set(PROMPT_SPECS))
        if unknown:
            names = ", ".join(f"{u}.md" for u in unknown)
            msg = (
                f"{directory} holds files that are not prompts of the pack: {names} "
                f"(known: {', '.join(sorted(PROMPT_SPECS))})"
            )
            raise ConfigError(msg)
        texts.update(override)
        origin = str(directory)
    pack = PromptPack(texts, origin)
    errors = pack_errors(pack)
    if errors:
        msg = f"The prompt pack from {origin} is not valid: " + "; ".join(errors)
        raise ConfigError(msg)
    return pack


@cache
def packaged_pack() -> PromptPack:
    """The pack shipped with Fusion, whatever ``FUSION_PROMPT_PACK`` says."""
    return load_prompt_pack(None)


@cache
def default_pack() -> PromptPack:
    """The pack in use unless a run picks one: the shipped one, or ``FUSION_PROMPT_PACK``."""
    override = os.environ.get(PACK_ENV, "").strip()
    return load_prompt_pack(Path(override).expanduser() if override else None)


_ACTIVE: ContextVar[PromptPack | None] = ContextVar("fusion_prompt_pack", default=None)


def active_pack() -> PromptPack:
    """The pack prompts are built from right now."""
    return _ACTIVE.get() or default_pack()


@contextmanager
def use_prompt_pack(pack: PromptPack) -> Iterator[PromptPack]:
    """Build prompts from ``pack`` inside the block (and in tasks started from it)."""
    token = _ACTIVE.set(pack)
    try:
        yield pack
    finally:
        _ACTIVE.reset(token)
