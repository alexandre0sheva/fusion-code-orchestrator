"""A tiny executable coding task, built in memory, for sandbox, scorer and pipeline tests."""

from __future__ import annotations

from typing import Any

from fusion.bench.patch import diff_files
from fusion.bench.spec import BenchTask

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
SQUARED = "def add(a, b):\n    return a * b\n"  # passes the visible test, fails two hidden ones
ABSOLUTE = "def add(a, b):\n    return abs(a) + abs(b)\n"  # fails only the negative-number test

VISIBLE = (
    "import unittest\n\nfrom calc import add\n\n\n"
    "class VisibleTests(unittest.TestCase):\n"
    "    def test_two_and_two(self):\n"
    "        self.assertEqual(add(2, 2), 4)\n"
)
HIDDEN = (
    "import unittest\n\nfrom calc import add\n\n\n"
    "class HiddenTests(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n\n"
    "    def test_negative(self):\n"
    "        self.assertEqual(add(-1, 1), 0)\n\n"
    "    def test_big(self):\n"
    "        self.assertEqual(add(10, 5), 15)\n"
)
EXPECTED = [f"tests.test_hidden.HiddenTests.{n}" for n in ("test_add", "test_big", "test_negative")]
FILES = {"calc.py": BUGGY, "tests/test_visible.py": VISIBLE}


def patch_for(source: str) -> str:
    """The unified diff taking ``calc.py`` from the buggy version to ``source``."""
    return diff_files(FILES, {**FILES, "calc.py": source})


def coding_task(**truth: Any) -> BenchTask:
    """A task asking for the fix of ``add``; ``truth`` keys override the defaults."""
    merged: dict[str, Any] = {
        "expected_pass": EXPECTED,
        "hidden_files": {"tests/test_hidden.py": HIDDEN},
        "timeout_s": 20,
        "solution": patch_for(FIXED),
        "solution_summary": "Make add return the sum",
        "flaws": [
            {"summary": "Make add return the product", "patch": patch_for(SQUARED)},
            {"summary": "Make add add absolute values", "patch": patch_for(ABSOLUTE)},
        ],
    }
    merged.update(truth)
    return BenchTask.model_validate(
        {
            "id": "tiny-add",
            "category": "coding",
            "prompt": "Fix `add` in calc.py so that it returns the sum of its arguments.",
            "files": FILES,
            "truth": merged,
        }
    )
