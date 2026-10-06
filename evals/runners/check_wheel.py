#!/usr/bin/env python3
"""Check that a built wheel carries everything a user without a clone needs.

    uv build && uv run python evals/runners/check_wheel.py dist/*.whl

A file that is in the source tree but missing from the wheel is how `uvx --from <wheel>` ends up
failing on a missing YAML, dashboard asset, integration template or dataset. It compares the
wheel with the repository (every non-bytecode file under ``src/fusion``, ``integrations`` and
``evals/datasets/v1``), the version with ``pyproject.toml``, and the console script.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# (source directory, where the wheel puts it); see [tool.hatch.build.targets.wheel] in pyproject.
TREES = (
    (Path("src") / "fusion", Path("fusion")),
    (Path("integrations"), Path("fusion") / "_integrations"),
    (Path("evals") / "datasets" / "v1", Path("fusion") / "bench" / "datasets" / "v1"),
)
_IGNORED = {".DS_Store"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def expected_files(repo: Path) -> dict[str, str]:
    """Wheel path -> the source file it comes from, for every file the wheel must hold."""
    found: dict[str, str] = {}
    for source, target in TREES:
        base = repo / source
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(base)
            if not path.is_file() or "__pycache__" in relative.parts:
                continue
            if path.name in _IGNORED or path.suffix in _IGNORED_SUFFIXES:
                continue
            found[(target / relative).as_posix()] = (source / relative).as_posix()
    return found


def _read(archive: zipfile.ZipFile, suffix: str) -> str | None:
    name = next((n for n in archive.namelist() if n.endswith(suffix)), None)
    return archive.read(name).decode("utf-8") if name else None


def problems(wheel: Path, repo: Path = REPO) -> list[str]:
    """What is wrong with ``wheel``, one line each; an empty list means it is complete."""
    found: list[str] = []
    version = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for target, source in expected_files(repo).items():
            if target not in names:
                found.append(f"missing from the wheel: {target} (from {source})")
        metadata = _read(archive, ".dist-info/METADATA") or ""
        built = re.search(r"^Version:\s*(\S+)", metadata, re.MULTILINE)
        if not built or built.group(1) != version:
            shown = built.group(1) if built else "none"
            found.append(f"wheel version {shown} does not match pyproject.toml {version}")
        entry_points = _read(archive, ".dist-info/entry_points.txt") or ""
        if not re.search(r"^fusion\s*=\s*fusion\.cli\.main:app\s*$", entry_points, re.MULTILINE):
            found.append("no `fusion` console script in the wheel's entry points")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--repo", type=Path, default=REPO)
    args = parser.parse_args(argv)
    found = problems(args.wheel, args.repo)
    for line in found:
        print(line, file=sys.stderr)
    if not found:
        print(f"{args.wheel.name}: complete ({len(expected_files(args.repo))} files checked)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
