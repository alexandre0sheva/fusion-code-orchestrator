"""The ``union`` aggregator: add the panel's findings together and keep what the input backs.

Lenses split a review or a debugging task by area, so each answer is a piece of one result. The
union collects every piece, merges duplicates (the same problem at the same place, as
``cluster_claims`` groups them) and checks each finding against the material the panel was given,
by string match, with no model call (Harness²'s fidelity criterion, done deterministically):

- **backed**: its evidence quote appears in the input, or it cites a file and line the input
  contains (a diff hunk, a file snippet, a stack-trace frame). Kept, most severe first.
- **unbacked**: its quote is not in the input and no cited line backs it, or it names a file the
  input never mentions. Dropped as invented, and counted.
- **unchecked**: it quotes nothing and cites no checkable line (an absence such as a missing test
  has nothing to quote). Kept in a section of its own; ``union+check`` asks one cheap model about
  these, and a confirmation counts only with a quote that passes the same string match.

Recommendations and tests are advice, not findings: they are merged and kept unless they cite an
invented quote or file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from fusion.config.loader import ModelEntry
from fusion.orchestration.claims import (
    FINDING_KINDS,
    Claim,
    ClaimCluster,
    PanelAnswer,
    cluster_claims,
)
from fusion.orchestration.ledger import CallGateway
from fusion.orchestration.prompts import (
    UNION_CHECK_SCHEMA,
    build_union_check_prompt,
    get_role_prompt,
)
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.routing.classifier import TaskType

__all__ = [
    "Material",
    "UnionFinding",
    "UnionResult",
    "UNION_CHECK_OUTPUT_TOKENS",
    "build_union",
    "check_claim",
    "check_unchecked",
    "merge_findings",
    "render_union",
    "union_claims",
]

Verdict = Literal["backed", "unbacked", "unchecked"]
MIN_QUOTE_CHARS = 4  # a shorter "quote" (a name, a symbol) matches by chance
UNION_CHECK_OUTPUT_TOKENS = 400  # the checker returns verdicts and short quotes, nothing else
LINE_TOLERANCE = 2  # models miscount lines by one or two
_SEVERITY_LEVEL = {"low": 0, "med": 1, "high": 2, "critical": 3}
_VERDICT_RANK = {"backed": 0, "unchecked": 1, "unbacked": 2}

_DIFF_FILE = re.compile(r"^(?:\+\+\+|---) (?:[ab]/)?(\S+)")
_DIFF_GIT = re.compile(r"^diff --git a/(\S+) b/(\S+)")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# path:line (app/db.py:42) and Python frames (File "app/db.py", line 42)
_PATH_LINE = re.compile(r"([\w./\\-]+\.[A-Za-z0-9]{1,6}):(\d+)")
_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)')


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def _clean_path(path: str) -> str:
    path = path.strip().strip("`'\"").replace("\\", "/")
    for prefix in ("a/", "b/", "./"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
    return path


@dataclass
class Material:
    """What the panel was given, indexed for the evidence check.

    ``files`` maps each path the input names to the line numbers it shows (diff hunks, snippet
    lines, stack-trace frames); an empty set means the file is named but no line of it is shown.
    """

    text: str = ""  # normalised for matching (whitespace collapsed, lower case)
    files: dict[str, set[int]] = field(default_factory=dict)
    source: str = ""  # the input as given, for the union+check prompt

    @classmethod
    def from_inputs(
        cls,
        *,
        primary: str = "",
        context: str = "",
        snippets: Sequence[str] = (),
        changed_files: Sequence[str] = (),
    ) -> Material:
        files: dict[str, set[int]] = {}
        for path in changed_files:
            files.setdefault(_clean_path(path), set())
        for text in (primary, context):
            _index_diff(text, files)
            _index_trace(text, files)
        for snippet in snippets:
            _index_snippet(snippet, files)
            _index_diff(snippet, files)
        texts = [primary, context, *snippets, *changed_files]
        source = "\n\n".join(t for t in texts if t)
        return cls(text=_norm(source), files=files, source=source)

    @property
    def empty(self) -> bool:
        """No input to check against: every finding is unchecked."""
        return not self.text and not self.files

    def has_quote(self, quote: str) -> bool | None:
        """Whether ``quote`` appears in the input; None when it is too short to tell.

        A quote with an ellipsis (``a ... b``) counts when every part of it appears.
        """
        pieces = [_norm(p).strip("`'\" ") for p in re.split(r"\.\.\.|…", quote)]
        pieces = [p for p in pieces if len(p) >= MIN_QUOTE_CHARS]
        if not pieces:
            return None
        return all(p in self.text for p in pieces)

    def lines_of(self, path: str) -> set[int] | None:
        """Line numbers the input shows for ``path``; None when the input never names the file."""
        wanted = _clean_path(path)
        if not wanted:
            return None
        for known, lines in self.files.items():
            if known == wanted or known.endswith("/" + wanted) or wanted.endswith("/" + known):
                return lines
        if _norm(wanted) in self.text:
            return set()  # named in prose, no lines shown
        return None


def _index_diff(text: str, files: dict[str, set[int]]) -> None:
    current: str | None = None
    for line in text.splitlines():
        git = _DIFF_GIT.match(line)
        if git:
            current = _clean_path(git.group(2))
            files.setdefault(current, set())
            continue
        header = _DIFF_FILE.match(line)
        if header and header.group(1) != "/dev/null":
            current = _clean_path(header.group(1))
            files.setdefault(current, set())
            continue
        hunk = _HUNK.match(line)
        if hunk and current is not None:
            old_start, old_len, new_start, new_len = hunk.groups()
            for start, length in ((old_start, old_len), (new_start, new_len)):
                first, count = int(start), int(length) if length is not None else 1
                files[current].update(range(first, first + count))


def _index_trace(text: str, files: dict[str, set[int]]) -> None:
    for pattern in (_PY_FRAME, _PATH_LINE):
        for path, line in pattern.findall(text):
            files.setdefault(_clean_path(path), set()).add(int(line))


def _index_snippet(snippet: str, files: dict[str, set[int]]) -> None:
    """A workspace snippet is ``path`` on the first line, then the file's text."""
    head, _, body = snippet.partition("\n")
    head = head.strip()
    if head and not any(c.isspace() for c in head) and ("/" in head or "." in head):
        lines = files.setdefault(_clean_path(head), set())
        lines.update(range(1, len(body.splitlines()) + 1))


def check_claim(claim: Claim, material: Material) -> tuple[Verdict, str]:
    """Whether ``material`` backs ``claim``, and why (see the module docstring)."""
    if material.empty:
        return "unchecked", "there was no input to check it against"
    quoted = material.has_quote(claim.evidence) if claim.evidence else None
    if quoted:
        return "backed", "its quote is in the input"
    if claim.file:
        lines = material.lines_of(claim.file)
        if lines is None:
            return "unbacked", f"{claim.file} is not in the input"
        if claim.line is not None and any(abs(claim.line - n) <= LINE_TOLERANCE for n in lines):
            return "backed", f"the input shows {claim.file}:{claim.line}"
    if quoted is False:
        return "unbacked", "its quote is not in the input"
    return "unchecked", "it quotes nothing the input can confirm"


@dataclass
class UnionFinding:
    """One merged point: the claim shown, its verdict, and who raised it."""

    cluster: ClaimCluster
    claim: Claim
    verdict: Verdict
    reason: str
    models: list[str]
    lenses: list[str]

    @property
    def advice(self) -> bool:
        return self.claim.kind not in FINDING_KINDS

    @property
    def severity_level(self) -> int:
        return _SEVERITY_LEVEL[self.claim.severity] if self.claim.severity else -1


@dataclass
class UnionResult:
    """The union of the panel's answers, sorted into what is kept and what is dropped."""

    backed: list[UnionFinding] = field(default_factory=list)
    unchecked: list[UnionFinding] = field(default_factory=list)
    advice: list[UnionFinding] = field(default_factory=list)
    dropped: list[UnionFinding] = field(default_factory=list)
    none_found: list[str] = field(default_factory=list)  # models that reported nothing
    lenses: dict[str, str] = field(default_factory=dict)  # model -> lens
    n_answers: int = 0
    checked_by: str = ""  # the union+check model, when one was asked

    @property
    def kept(self) -> list[UnionFinding]:
        return [*self.backed, *self.unchecked, *self.advice]

    def kept_clusters(self) -> list[ClaimCluster]:
        """The clusters the union keeps, each showing the claim it chose (for structured output)."""
        out = []
        for item in self.kept:
            out.append(
                item.cluster.model_copy(
                    update={
                        "text": item.claim.text,
                        "severity": item.claim.severity,
                        "file": item.claim.file,
                        "line": item.claim.line,
                    }
                )
            )
        return out

    def summary(self) -> dict[str, object]:
        """Counts for the run record."""
        return {
            "backed": len(self.backed),
            "unchecked": len(self.unchecked),
            "advice": len(self.advice),
            "dropped": len(self.dropped),
            "none_found": list(self.none_found),
            "lenses": dict(self.lenses),
            "checked_by": self.checked_by or None,
        }


def _judge_cluster(
    cluster: ClaimCluster, material: Material, lenses: Mapping[str, str]
) -> UnionFinding:
    judged = [(member, *check_claim(member.claim, material)) for member in cluster.members]
    best = min(_VERDICT_RANK[verdict] for _, verdict, _ in judged)
    verdict = next(v for _, v, _ in judged if _VERDICT_RANK[v] == best)
    pool = [(m, r) for m, v, r in judged if v == verdict]
    lead, reason = max(pool, key=lambda item: _claim_rank(item[0].claim))
    severities = [
        _SEVERITY_LEVEL[m.claim.severity]
        for m, v, _ in judged
        if m.claim.severity and v != "unbacked"
    ]
    claim = lead.claim
    if severities and max(severities) > _SEVERITY_LEVEL.get(claim.severity or "", -1):
        top = next(k for k, v in _SEVERITY_LEVEL.items() if v == max(severities))
        claim = claim.model_copy(update={"severity": top})
    models = sorted({m.model for m in cluster.members})
    return UnionFinding(
        cluster=cluster,
        claim=claim,
        verdict=verdict,
        reason=reason,
        models=models,
        lenses=sorted({lenses[m] for m in models if m in lenses}),
    )


def _claim_rank(claim: Claim) -> tuple[int, int, int]:
    level = _SEVERITY_LEVEL[claim.severity] if claim.severity else -1
    return (level, int(bool(claim.evidence)), len(claim.text))


def _order(items: Iterable[UnionFinding]) -> list[UnionFinding]:
    return sorted(items, key=lambda f: (-f.severity_level, -len(f.models), f.cluster.id))


def merge_findings(
    answers: Mapping[str, PanelAnswer],
    material: Material | None = None,
    *,
    lenses: Mapping[str, str] | None = None,
    clusters: list[ClaimCluster] | None = None,
) -> UnionResult:
    """Merge the answers' claims and sort them by what the input backs (no model call).

    ``clusters`` reuses a clustering already made of ``answers``. Without ``material`` nothing can
    be checked, so every finding is unchecked.
    """
    material = material or Material()
    lens_of = dict(lenses or {})
    result = UnionResult(
        none_found=sorted(m for m, a in answers.items() if a.none_found),
        lenses={m: lens_of[m] for m in answers if m in lens_of},
        n_answers=len(answers),
    )
    for cluster in clusters if clusters is not None else cluster_claims(answers):
        item = _judge_cluster(cluster, material, lens_of)
        if item.verdict == "unbacked":
            result.dropped.append(item)
        elif item.advice:
            result.advice.append(item)
        elif item.verdict == "backed":
            result.backed.append(item)
        else:
            result.unchecked.append(item)
    result.backed = _order(result.backed)
    result.unchecked = _order(result.unchecked)
    result.advice = _order(result.advice)
    result.dropped = _order(result.dropped)
    return result


def union_claims(
    answers: Mapping[str, PanelAnswer],
    material: Material | None = None,
    *,
    lenses: Mapping[str, str] | None = None,
) -> list[Claim]:
    """Every claim the union keeps, merged: backed findings (most severe first), then unchecked
    findings, then advice. Claims the input contradicts are left out."""
    return [item.claim for item in merge_findings(answers, material, lenses=lenses).kept]


# ------------------------------------------------------------------------------------- display


def _where(claim: Claim) -> str:
    if not claim.file:
        return ""
    return f" ({claim.file}:{claim.line})" if claim.line else f" ({claim.file})"


def _source(item: UnionFinding) -> str:
    if item.lenses:
        return ", ".join(item.lenses) + f" ({', '.join(item.models)})"
    return ", ".join(item.models)


def _line(item: UnionFinding) -> str:
    claim = item.claim
    severity = f"[{claim.severity}] " if claim.severity else ""
    quote = f'; quote: "{claim.evidence.strip()}"' if claim.evidence else ""
    return f"- {severity}{claim.text}{_where(claim)}, {claim.kind}; from {_source(item)}{quote}"


def render_union(result: UnionResult) -> str:
    """The union as Markdown, for Claude Code."""
    by = "lenses" if len(set(result.lenses.values())) >= 2 else "models"
    count = len(set(result.lenses.values())) if by == "lenses" else result.n_answers
    noun = "finding" if len(result.backed) == 1 else "findings"
    called = f"{result.checked_by} checked the unquoted ones" if result.checked_by else (
        "no merge model was called"
    )
    lines = [
        f"## Panel union: {len(result.backed)} {noun} backed by the input, from {count} {by}; "
        f"{called}",
    ]
    if by == "lenses":
        held = ", ".join(f"{lens} ({model})" for model, lens in sorted(result.lenses.items()))
        lines.extend(["", f"Each model covered one area: {held}."])
    if result.none_found:
        found_nothing = [
            f"{result.lenses[m]} ({m})" if m in result.lenses else m for m in result.none_found
        ]
        lines.append(f"Nothing to report from: {', '.join(found_nothing)}.")
    sections = (
        ("Findings backed by the input", result.backed),
        (
            "Findings not checked against the input (no quote or line to check; verify before "
            "relying on them)",
            result.unchecked,
        ),
        ("Suggested changes and tests", result.advice),
    )
    for title, items in sections:
        if items:
            lines.extend(["", f"### {title}", *(_line(item) for item in items)])
    if not result.kept:
        lines.extend(["", "No finding survived: the panel reported nothing the input backs."])
    if result.dropped:
        reasons = "; ".join(
            f"{item.claim.text} ({item.reason})" for item in result.dropped[:5]
        )
        more = f" and {len(result.dropped) - 5} more" if len(result.dropped) > 5 else ""
        lines.extend(
            [
                "",
                f"Left out {len(result.dropped)} point(s) the input does not back: {reasons}"
                f"{more}.",
            ]
        )
    return "\n".join(lines)


def build_union(result: UnionResult) -> ModelResponse:
    """The union as the run's final answer (provider ``fusion``, model ``union``)."""
    return ModelResponse(provider="fusion", model="union", text=render_union(result))


# ------------------------------------------------------------------------------- union + check


def apply_check(
    result: UnionResult, verdicts: Mapping[str, tuple[str, str | None]], material: Material
) -> int:
    """Settle unchecked findings with a checker's verdicts (cluster id -> (verdict, quote)).

    ``confirmed`` moves a finding to backed only when its quote passes the string match;
    ``rejected`` drops it; anything else leaves it unchecked. Returns how many changed.
    """
    still: list[UnionFinding] = []
    changed = 0
    for item in result.unchecked:
        verdict, quote = verdicts.get(item.cluster.id, ("unsure", None))
        if verdict == "confirmed" and quote and material.has_quote(quote):
            item.claim = item.claim.model_copy(update={"evidence": quote})
            item.verdict, item.reason = "backed", "a checker quoted the input"
            result.backed.append(item)
            changed += 1
        elif verdict == "rejected":
            item.verdict, item.reason = "unbacked", "a checker found the input does not show it"
            result.dropped.append(item)
            changed += 1
        else:
            still.append(item)
    result.unchecked = still
    result.backed = _order(result.backed)
    result.dropped = _order(result.dropped)
    return changed


def check_lines(result: UnionResult) -> list[tuple[str, str]]:
    """``(cluster id, line)`` for each unchecked finding, for the checker's prompt."""
    return [
        (item.cluster.id, f"{item.claim.text}{_where(item.claim)}") for item in result.unchecked
    ]


async def check_unchecked(
    result: UnionResult,
    material: Material,
    *,
    checker_model: str,
    registry_models: dict[str, ModelEntry],
    task_type: TaskType,
    gateway: CallGateway,
) -> ModelResponse | None:
    """``union+check``: one cheap call settles the unchecked findings (see ``apply_check``).

    Returns the checker's response, or None when there was nothing to check. A failed call
    leaves the findings unchecked; the caller reads ``response.error``.
    """
    findings = check_lines(result)
    if not findings:
        return None
    entry = registry_models[checker_model]
    request = ModelRequest(
        model_id=entry.model_id,
        system_prompt=get_role_prompt("judge"),
        user_prompt=build_union_check_prompt(
            task_type=task_type, material=material.source, findings=findings
        ),
        max_tokens=min(entry.max_tokens, UNION_CHECK_OUTPUT_TOKENS),
        response_schema=UNION_CHECK_SCHEMA,
        response_schema_name="union_check",
        metadata={"task_type": task_type.value, "role": "union_check", "personality": "judge"},
    )
    response = await gateway.call(stage="synthesis", alias=checker_model, request=request)
    if not response.error:
        apply_check(result, _verdicts(response), material)
        result.checked_by = checker_model
    return response


def _verdicts(response: ModelResponse) -> dict[str, tuple[str, str | None]]:
    data: Any = response.parsed_json
    if data is None:
        text = response.content
        start, end = text.find("{"), text.rfind("}") + 1
        try:
            data = json.loads(text[start:end]) if 0 <= start < end else None
        except json.JSONDecodeError:
            data = None
    rows = data.get("verdicts") if isinstance(data, dict) else None
    verdicts: dict[str, tuple[str, str | None]] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            quote = row.get("quote")
            verdicts[row["id"]] = (
                str(row.get("verdict") or "unsure"),
                quote if isinstance(quote, str) else None,
            )
    return verdicts
