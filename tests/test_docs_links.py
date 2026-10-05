"""Docs hygiene: relative links resolve, canonical docs exist, README stays short."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_FENCE = re.compile(r"^\s*(```|~~~)")
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")

CANONICAL_DOCS = [
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/ARCHITECTURE.md",
    "docs/CONFIGURATION.md",
    "docs/COSTS.md",
    "docs/BENCHMARKING.md",
    "docs/INTEGRATIONS.md",
    "docs/CLAUDE_CODE_AB.md",
]


def _markdown_files() -> list[Path]:
    files = [ROOT / name for name in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md")]
    files += [ROOT / "SECURITY.md", ROOT / "CLAUDE.md", ROOT / "plugin" / "README.md"]
    files += sorted((ROOT / "docs").glob("*.md"))
    return [path for path in files if path.exists()]


def _slug(heading: str) -> str:
    text = re.sub(r"[`*_]", "", heading).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text)


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADING.match(line)
        if match:
            anchors.add(_slug(match.group(1)))
    return anchors


def _links(path: Path) -> list[str]:
    links: list[str] = []
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            links.extend(_LINK.findall(re.sub(r"`[^`]*`", "", line)))
    return links


@pytest.mark.parametrize("doc", CANONICAL_DOCS)
def test_canonical_doc_exists(doc: str) -> None:
    assert (ROOT / doc).is_file(), f"{doc} is part of the docs contract (see CONTRIBUTING.md)"


def test_readme_stays_short() -> None:
    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 200, f"README.md has {len(lines)} lines; move detail into docs/"


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(path: Path) -> None:
    problems: list[str] = []
    for link in _links(path):
        if re.match(r"^(https?:|mailto:)", link):
            continue
        target, _, anchor = link.partition("#")
        resolved = path if not target else (path.parent / target).resolve()
        if not resolved.exists():
            problems.append(f"{link} -> missing file")
        elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
            problems.append(f"{link} -> missing anchor")
    assert not problems, f"{path.relative_to(ROOT)}: " + "; ".join(problems)
