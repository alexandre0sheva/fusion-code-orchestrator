"""``ReviewScorer``: recall and precision against the bugs seeded in a code-review task.

The answer's findings are the places it points at (``file:line``, ``file, line N``, or a bare
``line N`` when the task has one file). A finding reports a seeded bug when it names the bug's
file within ``line_tolerance`` lines of it: in full when it also says what kind of bug it is, at
half credit when it does not. Findings that match nothing are false positives; one that names a
file the task does not contain is a hallucinated file and counts double. Only bugs left unmatched
are put to an LLM judge (the first of ``judge_models``), together with the findings left over.
A finding with no location cannot be called false without a judge, so it is never penalised.

A task with no bugs is a clean change: quality is ``1 / (1 + false-positive weight)``, so one
false alarm on clean code leaves it at 0.5, below the pass mark.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fusion.bench.scoring.base import (
    AnswerView,
    Evidence,
    ScoreEnv,
    ScoreResult,
    ask_judge,
    estimate_judge_call,
    normalize,
    segments,
)
from fusion.bench.scoring.points import points_fallback
from fusion.bench.spec import BenchTask, ReviewTruth, SeededBug
from fusion.routing.budget import PlannedCall
from fusion.security.untrusted import wrap_untrusted

__all__ = ["ReviewScorer", "extract_findings"]

SEVERITY_WEIGHT = {"critical": 3.0, "high": 2.0, "medium": 1.0, "low": 0.5}
LOCATION_ONLY_CREDIT = 0.5  # the finding is at the bug but never says what kind of bug it is
HALLUCINATED_FILE_WEIGHT = 2.0  # a false positive in a file that does not exist counts double
_MAX_JUDGED_FINDINGS = 25

_AT = r"(?:[:#]\s?L?|[\s,(]+lines?\s*L?)"
_FILE_LINE = re.compile(
    rf"(?P<file>[\w@./\\-]*[\w-]\.[A-Za-z][A-Za-z0-9]{{0,5}}){_AT}(?P<line>\d+)"
    r"(?:\s*[-–]\s*L?(?P<end>\d+))?"
)
_BARE_LINE = re.compile(r"\blines?\s+(?P<line>\d+)(?:\s*[-–]\s*(?P<end>\d+))?", re.I)


@dataclass
class Finding:
    """One place the answer points at, with the item of the answer that says why."""

    index: int  # the item it came from
    text: str
    file: str | None  # the task's file it names; None when it names none, or one that is not there
    line: int | None
    end: int | None
    named: str | None = None  # the file as the answer wrote it
    hallucinated: bool = False

    @property
    def located(self) -> bool:
        return self.file is not None and self.line is not None

    def spans(self, line: int, tolerance: int) -> bool:
        if self.line is None:
            return False
        last = self.end if self.end is not None and self.end >= self.line else self.line
        return self.line <= line + tolerance and last >= line - tolerance


def _resolve(name: str, files: list[str]) -> str | None:
    clean = name.replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    candidates = [clean, *([clean[2:]] if clean[:2] in ("a/", "b/") else [])]
    for candidate in candidates:
        if candidate in files:
            return candidate
    for candidate in candidates:
        near = [f for f in files if f.endswith("/" + candidate) or candidate.endswith("/" + f)]
        if near:
            return near[0]
    return None


def extract_findings(text: str, files: list[str]) -> list[Finding]:
    """Every location the answer points at. With no files to check against (``files`` empty) a
    named file is taken at its word and nothing can be called hallucinated."""
    found: list[Finding] = []
    for index, item in enumerate(segments(text)):
        for m in _FILE_LINE.finditer(item):
            named = m["file"]
            resolved = _resolve(named, files) if files else named.replace("\\", "/")
            line = int(m["line"])
            end = int(m["end"]) if m["end"] else None
            found.append(
                Finding(index, item, resolved, line, end, named, hallucinated=resolved is None)
            )
        rest = _FILE_LINE.sub(" ", item)
        if len(files) == 1 and not found_in(found, index):
            for m in _BARE_LINE.finditer(rest):
                end = int(m["end"]) if m["end"] else None
                found.append(Finding(index, item, files[0], int(m["line"]), end, files[0]))
        if not found_in(found, index):
            found.append(Finding(index, item, None, None, None))
    return found


def found_in(found: list[Finding], index: int) -> bool:
    return any(f.index == index for f in found)


def _says(text: str, bug: SeededBug) -> bool:
    haystack = normalize(text)
    return any(normalize(name) in haystack for name in (bug.category, *bug.aliases))


class ReviewScorer:
    """Seeded-bug recall and precision; see the module doc."""

    name = "review"

    def estimate_calls(self, task: BenchTask, judges: list[str]) -> list[PlannedCall]:
        if not task.truth.get("bugs") or not judges:
            return []
        return [estimate_judge_call(task, judges[0], extra_tokens=800)]

    async def score(
        self,
        task: BenchTask,
        answer: AnswerView,
        env: ScoreEnv,
        evidence: Evidence | None = None,
    ) -> ScoreResult:
        if "bugs" not in task.truth:
            return await points_fallback(task, self.name).score(task, answer, env, evidence)
        truth = ReviewTruth.model_validate(task.truth)
        spent = env.spent_usd()
        findings = extract_findings(answer.text(), list(task.files))
        bugs = sorted(enumerate(truth.bugs, 1), key=lambda ib: -SEVERITY_WEIGHT[ib[1].severity])

        credit: dict[int, float] = {}
        how: dict[int, str] = {}
        taken: set[int] = set()  # indexes into ``findings``
        for number, bug in bugs:
            near = [
                (i, f)
                for i, f in enumerate(findings)
                if i not in taken
                and f.file == bug.file
                and f.line is not None
                and f.spans(bug.line, truth.line_tolerance)
            ]
            if not near:
                continue
            near.sort(key=lambda p: (not _says(p[1].text, bug), abs((p[1].line or 0) - bug.line)))
            i, finding = near[0]
            taken.add(i)
            said = _says(finding.text, bug)
            credit[number] = 1.0 if said else LOCATION_ONLY_CREDIT
            how[number] = "location+category" if said else "location"

        # What is left, once findings at a matched bug's location (repeats of it) are set aside.
        duplicates = [
            i
            for i, f in enumerate(findings)
            if i not in taken
            and f.located
            and any(
                f.file == b.file and f.spans(b.line, truth.line_tolerance)
                for n, b in bugs
                if n in credit
            )
        ]
        leftover = [n for n, _ in bugs if n not in credit]
        offered = [
            i
            for i, f in enumerate(findings)
            if i not in taken and i not in duplicates and not f.hallucinated
        ][:_MAX_JUDGED_FINDINGS]
        if leftover and offered and env.judge_models:
            matched = await self._judge_matches(task, truth, findings, leftover, offered, env)
            for number, i in matched.items():
                credit[number] = 1.0
                how[number] = "judge"
                taken.add(i)

        false_positives = [
            f
            for i, f in enumerate(findings)
            if f.line is not None and i not in taken and i not in duplicates
        ]
        weight_fp = sum(
            HALLUCINATED_FILE_WEIGHT if f.hallucinated else 1.0 for f in false_positives
        )
        total = sum(SEVERITY_WEIGHT[b.severity] for _, b in bugs)
        recall = (
            sum(SEVERITY_WEIGHT[truth.bugs[n - 1].severity] * c for n, c in credit.items()) / total
            if total
            else 1.0
        )
        hits = sum(credit.values())
        precision = hits / (hits + weight_fp) if hits + weight_fp else 1.0
        if not bugs:
            quality = 1.0 / (1.0 + weight_fp)
        elif precision + recall:
            quality = 2 * precision * recall / (precision + recall)
        else:
            quality = 0.0
        details: dict[str, Any] = {
            "matched": [
                {"bug": n, "file": truth.bugs[n - 1].file, "how": how[n], "credit": credit[n]}
                for n in sorted(credit)
            ],
            "missed": [n for n, _ in bugs if n not in credit],
            "false_positives": [
                {"file": f.named, "line": f.line, "hallucinated": f.hallucinated}
                for f in false_positives
            ],
            "hallucinated_files": sorted(
                {f.named or "" for f in false_positives if f.hallucinated}
            ),
            "duplicates": len(duplicates),
            "unlocated_findings": sum(1 for f in findings if f.line is None),
            "recall": recall,
            "precision": precision,
            "clean_task": not bugs,
        }
        if env.errors:
            details["judge_errors"] = list(env.errors)
        return ScoreResult(
            quality=quality,
            scorer=self.name,
            details=details,
            cost_usd=env.spent_usd() - spent,
        )

    async def _judge_matches(
        self,
        task: BenchTask,
        truth: ReviewTruth,
        findings: list[Finding],
        leftover: list[int],
        offered: list[int],
        env: ScoreEnv,
    ) -> dict[int, int]:
        """``{bug number: finding index}`` for bugs the judge says a finding reports."""
        bug_lines = [
            {
                "id": f"b{n}",
                "file": truth.bugs[n - 1].file,
                "line": truth.bugs[n - 1].line,
                "description": truth.bugs[n - 1].description,
            }
            for n in leftover
        ]
        finding_lines = [{"id": i, "text": findings[i].text[:600]} for i in offered]
        prompt = (
            "A code review should have found the defects below. For each defect, say which of "
            "the reviewer's findings reports it (the same defect, wherever the reviewer put it), "
            "or null when none does. A finding may report at most one defect.\n\n"
            f"Defects:\n{_lines(bug_lines)}\n\n"
            f"Reviewer's findings:\n{wrap_untrusted(_lines(finding_lines))}\n\n"
            'Answer: {"matches": [{"defect": "b1", "finding": 3 or null, "quote": "..."}]}'
        )
        data = await ask_judge(
            env,
            env.judge_models[0],
            kind="match",
            prompt=prompt,
            payload={"bugs": bug_lines, "findings": finding_lines},
        )
        matched: dict[int, int] = {}
        for row in (data or {}).get("matches", []):
            if not isinstance(row, dict):
                continue
            defect, finding = str(row.get("defect", "")), row.get("finding")
            number = int(defect[1:]) if re.fullmatch(r"b\d+", defect) else 0
            if number in leftover and finding in offered and finding not in matched.values():
                matched.setdefault(number, int(finding))
        return matched


def _lines(rows: list[dict[str, Any]]) -> str:
    return "\n".join("- " + ", ".join(f"{k}: {v}" for k, v in row.items()) for row in rows)
