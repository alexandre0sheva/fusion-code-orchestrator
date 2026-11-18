"""What a panel member writes for verified generation: a patch, the checks that judge it, and
guardrails.

Under ``aggregator: verified-synth`` every member answers a coding task with a **complete** patch
(from its approach: a minimal fix, a root-cause fix) **and** one to three check scripts that would
fail on the code as it is and pass once it is fixed correctly (``CheckScript``), plus short
guardrails: invariants in prose ("the public signature of X is unchanged"). The checks are
scripts, not prose, because a script is the cheap, enforceable form: prose can be skipped, a check
that fails cannot (Harness²). ``fusion.verify.crosscheck`` then runs every patch against every
member's checks in a sandbox copy.

The **evidence boundary** lives here too: what grades a task (hidden tests, the reference solution
and its wrong twins, rubrics) must never reach a prompt that proposes an answer, or the panel
would be graded on what it was shown. ``grader_material`` lists those texts for a task and
``assert_no_grader_material`` fails when a prompt contains any of them (Harness²'s
``assert_no_grader_verdicts``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from fusion.orchestration.claims import PanelAnswer

__all__ = [
    "MAX_CHECKS",
    "MAX_GUARDRAILS",
    "TASK_AUTHOR",
    "Candidate",
    "CheckKind",
    "CheckLanguage",
    "CheckScript",
    "EvidenceLeakError",
    "assert_no_grader_material",
    "candidates_from_answers",
    "checks_instruction",
    "grader_material",
    "parse_checks",
    "parse_guardrails",
]

CheckLanguage = Literal["python", "shell"]
CheckKind = Literal["repro", "property", "invariant"]
MAX_CHECKS = 3  # per member; extra checks are ignored
MAX_GUARDRAILS = 5
MAX_GUARDRAIL_CHARS = 200
TASK_AUTHOR = "task"  # the author of checks that come with the task (its visible tests)
_MIN_MATERIAL_CHARS = 16  # shorter lines are too generic to prove a leak


class CheckScript(BaseModel):
    """One executable check a panel member wrote with its patch.

    ``repro`` reproduces the reported problem and ``property`` states what correct code must do:
    both must FAIL on the code as it is. ``invariant`` states what must stay true (a guardrail in
    code form): it must PASS on the code as it is, and still pass after the patch.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="Short identifier: letters, digits, '_' or '-'.")
    language: CheckLanguage = Field(
        description="python (run with the project's Python, from the project root) or shell (bash)."
    )
    kind: CheckKind = Field(
        default="repro",
        description=(
            "repro or property: fails on the current code and passes once it is fixed; "
            "invariant: passes now and must still pass after the fix."
        ),
    )
    body: str = Field(
        description=(
            "The whole script. Exit 0 means the check passes, anything else that it fails. "
            "No network, no writes outside the project directory, no external packages."
        )
    )


@dataclass
class Candidate:
    """One member's proposal: its complete patch, its checks and its guardrails. ``id`` is the
    member (a catalog alias); identical patches from several members stay separate candidates."""

    id: str
    patch: str
    checks: list[CheckScript] = field(default_factory=list)
    guardrails: list[str] = field(default_factory=list)


def parse_checks(raw: Any) -> list[CheckScript]:
    """The checks in a model's reply (the ``checks`` field), leniently: entries that are not a
    check are skipped, and at most ``MAX_CHECKS`` are kept."""
    if not isinstance(raw, list):
        return []
    checks: list[CheckScript] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            check = CheckScript.model_validate(item)
        except ValidationError:
            continue
        if check.body.strip() and check.name.strip():
            checks.append(check)
        if len(checks) == MAX_CHECKS:
            break
    return checks


def parse_guardrails(raw: Any) -> list[str]:
    """The guardrails in a model's reply: short, non-empty strings, at most ``MAX_GUARDRAILS``."""
    if not isinstance(raw, list):
        return []
    lines = [str(g).strip()[:MAX_GUARDRAIL_CHARS] for g in raw if isinstance(g, str | int | float)]
    return [g for g in lines if g][:MAX_GUARDRAILS]


def candidates_from_answers(answers: Mapping[str, PanelAnswer]) -> list[Candidate]:
    """One candidate per member whose answer holds a patch, in panel order."""
    return [
        Candidate(id=model, patch=a.patch, checks=list(a.checks), guardrails=list(a.guardrails))
        for model, a in answers.items()
        if a.patch
    ]


def checks_instruction() -> str:
    """Appended to each member's prompt under ``verified-synth``: write checks with the patch."""
    return "\n".join(
        [
            "\n\n## Checks",
            "Your patch will be run against checks written by you AND by the other panelists, in a "
            "sandbox copy of the project. With the patch, also return in `checks` one to three "
            f"scripts (at most {MAX_CHECKS}) that judge ANY fix of this task, not only yours:",
            "- `repro` or `property`: FAILS on the code as it is now and PASSES once the task is "
            "done correctly. A check that already passes is discarded: it cannot tell a fix from "
            "no fix.",
            "- `invariant`: passes now and must still pass after a correct fix (behaviour the fix "
            "must not break).",
            "Each check is a whole script: `python` (run from the project root, which is on the "
            "import path) or `shell` (bash). Exit code 0 means pass. Standard library only, no "
            "network, no writes outside the project directory, and quick (a few seconds). Do not "
            "test your own patch's internals: test what the task asks for.",
            "In `guardrails` list up to five short invariants a correct fix keeps (for example "
            "'the public signature of parse() is unchanged').",
        ]
    )


# -- the evidence boundary ---------------------------------------------------------------------


class EvidenceLeakError(AssertionError):
    """A prompt contains grader material (a hidden test, the reference solution, a rubric)."""


def _added_lines(patch: str) -> list[str]:
    lines = []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
        elif not line.startswith(("-", "@@", " ", "===", "diff ", "index ")):
            lines.append(line)  # file blobs: every line is new content
    return lines


def _distinct(lines: Iterable[str], seen: str) -> list[str]:
    """Lines long enough to be telling and absent from what the model may see anyway."""
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if len(line) >= _MIN_MATERIAL_CHARS and line not in seen and line not in out:
            out.append(line)
    return out


def grader_material(truth: Mapping[str, Any], visible: Mapping[str, str]) -> dict[str, list[str]]:
    """What grades a task and must stay out of every proposer prompt, as telling lines by source.

    ``truth`` is a task's ground truth (``BenchTask.truth``), ``visible`` the files the model is
    shown: a line that also appears there proves nothing and is left out.
    """
    seen = "\n".join(visible.values())
    material: dict[str, list[str]] = {}
    hidden = truth.get("hidden_files") or {}
    for path, content in hidden.items():
        material[f"hidden test {path}"] = _distinct(str(content).splitlines(), seen)
    if truth.get("expected_pass"):
        material["hidden test ids"] = _distinct(map(str, truth["expected_pass"]), seen)
    if truth.get("solution"):
        material["reference solution"] = _distinct(_added_lines(str(truth["solution"])), seen)
    texts = [str(truth.get("solution_summary") or "")]
    for flaw in truth.get("flaws") or []:
        if isinstance(flaw, Mapping):
            texts.append(str(flaw.get("summary") or ""))
    for key in ("rubric", "soft_criteria", "hard_gates"):
        value = truth.get(key)
        if isinstance(value, list):
            texts += [str(v.get("text") or v) if isinstance(v, Mapping) else str(v) for v in value]
        elif value:
            texts.append(str(value))
    material["rubric and summaries"] = _distinct(texts, seen)
    return {source: lines for source, lines in material.items() if lines}


def assert_no_grader_material(prompt: str, material: Mapping[str, list[str]]) -> None:
    """Raise ``EvidenceLeakError`` when ``prompt`` contains any line of ``material``."""
    squashed = re.sub(r"\s+", " ", prompt)
    for source, lines in material.items():
        for line in lines:
            if re.sub(r"\s+", " ", line) in squashed:
                msg = f"a prompt contains grader material ({source}): {line[:80]!r}"
                raise EvidenceLeakError(msg)
