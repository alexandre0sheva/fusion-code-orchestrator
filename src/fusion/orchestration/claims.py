"""Claims: what panelists say as typed data, and what that says about how far they agree.

Panelists answer with a ``PanelAnswer`` (a summary plus atomic ``Claim`` objects). Claims from
different models are grouped by ``cluster_claims`` using only structure and normalized text: the
same file within a few lines, the same kind, similar wording. ``agreement_score`` turns the
clusters into consensus, unique and contradicted sets, and ``calibrated_confidence`` turns that
into one number. Nothing here reads keywords to judge quality; confidence follows agreement,
evidence and coverage.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from fusion.providers.base import inline_schema_refs

ClaimKind = Literal["finding", "hypothesis", "recommendation", "risk", "test"]
Severity = Literal["low", "med", "high", "critical"]

CLAIM_KINDS: tuple[str, ...] = ("finding", "hypothesis", "recommendation", "risk", "test")
_SEVERITY_LEVEL: dict[str, int] = {"low": 0, "med": 1, "high": 2, "critical": 3}
_SEVERITY_ALIASES = {"medium": "med", "moderate": "med", "minor": "low", "severe": "critical"}
_KIND_ALIASES = {
    "issue": "finding",
    "bug": "finding",
    "problem": "finding",
    "cause": "hypothesis",
    "fix": "recommendation",
    "suggestion": "recommendation",
    "change": "recommendation",
    "step": "recommendation",
    "concern": "risk",
}
# Kind given to claims scraped from prose, which carries no kind of its own.
_DEFAULT_KIND: dict[str, ClaimKind] = {
    "code_review": "finding",
    "debugging": "hypothesis",
    "architecture_decision": "recommendation",
    "implementation_plan": "recommendation",
    "answer_eval": "finding",
    "default": "recommendation",
}

# Clustering thresholds (token Jaccard similarity of normalized claim text).
LINE_WINDOW = 3  # claims on the same file this many lines apart are the same place
SIMILARITY = 0.5  # wording alone merges two claims of the same kind at this similarity
SIMILARITY_SAME_FILE = 0.34  # ... or at this similarity when they cite the same file
# Confidence weights: agreement between models, evidence behind the claims, answer coverage.
W_AGREEMENT, W_EVIDENCE, W_COVERAGE = 0.5, 0.3, 0.2
CONTRADICTION_PENALTY = 0.15
LOW_INFORMATION_CAP = 0.5  # one model cannot agree with anyone

_STOPWORDS = frozenset(
    {"a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "is", "are", "be", "been"}
    | {"it", "its", "this", "that", "with", "as", "at", "by", "from", "can", "should", "would"}
    | {"could", "will", "may", "might", "do", "does", "not", "no", "if", "then", "than", "so"}
    | {"such", "there", "their"}
)


class Claim(BaseModel):
    """One atomic statement a panelist makes."""

    id: str = ""
    text: str
    kind: ClaimKind
    severity: Severity | None = None
    file: str | None = None
    line: int | None = None
    evidence: str | None = None

    @property
    def has_evidence(self) -> bool:
        located = self.file is not None and self.line is not None
        return bool((self.evidence or "").strip()) or located


def patch_text_key(text: str) -> str:
    """A patch with line endings and surrounding blank space normalised, for comparing patches."""
    return "\n".join(line.rstrip() for line in text.strip().replace("\r\n", "\n").splitlines())


class PanelAnswer(BaseModel):
    """A panelist's whole answer."""

    summary: str = ""
    claims: list[Claim] = Field(default_factory=list)
    patch: str | None = Field(
        default=None,
        description=(
            "Only when the task asks for a code change: the whole change as ONE patch, a unified "
            "diff or the complete new content of each changed file under a `=== path ===` line. "
            "Otherwise null."
        ),
    )
    confidence: float = 0.5  # the model's own estimate; reported, never used for confidence
    score: float | None = None  # answer_eval only: 0-1 quality of the answer under evaluation


class ClusterMember(BaseModel):
    model: str
    claim: Claim


class ClaimCluster(BaseModel):
    """Claims from different models that say the same thing about the same place."""

    id: str
    kind: ClaimKind
    text: str
    severity: Severity | None = None
    file: str | None = None
    line: int | None = None
    models: list[str]
    members: list[ClusterMember]
    status: Literal["consensus", "unique", "contradicted", "partial"] = "unique"

    @property
    def support(self) -> int:
        return len(self.models)

    @property
    def severity_spread(self) -> int:
        levels = [_SEVERITY_LEVEL[m.claim.severity] for m in self.members if m.claim.severity]
        return max(levels) - min(levels) if levels else 0


class AgreementReport(BaseModel):
    """How far the panel agrees, and how much that can be trusted."""

    n_models: int
    n_requested: int
    n_clusters: int = 0
    score: float = 0.0  # 0 = nothing shared, 1 = every claim backed by every model
    consensus: list[str] = Field(default_factory=list)  # cluster ids backed by a majority
    unique: list[str] = Field(default_factory=list)  # backed by one model
    contradicted: list[str] = Field(default_factory=list)  # backed, but severities split
    outliers: list[str] = Field(default_factory=list)  # models sharing nothing with the others
    evidence_rate: float = 0.0  # share of claims with evidence or a file and line
    coverage: float = 0.0  # models that answered / models asked
    structured_share: float = 0.0  # answers that arrived as valid claims
    low_information: bool = True  # fewer than two answers: agreement cannot be measured
    confidence: float = 0.0


# --------------------------------------------------------------------------------------- schema


def panel_answer_schema() -> dict[str, Any]:
    """JSON Schema for ``PanelAnswer`` in the form providers' strict structured output accepts."""
    schema = inline_schema_refs(PanelAnswer.model_json_schema())

    def strict(node: Any) -> Any:
        if isinstance(node, list):
            return [strict(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {k: strict(v) for k, v in node.items() if k not in {"title", "default"}}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    result: dict[str, Any] = strict(schema)
    return result


# -------------------------------------------------------------------------------------- parsing


def _extract_json(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_claim(raw: Any, default_kind: ClaimKind) -> Claim | None:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    kind = str(raw.get("kind") or "").lower()
    kind = _KIND_ALIASES.get(kind, kind)
    severity = str(raw.get("severity") or "").lower()
    severity = _SEVERITY_ALIASES.get(severity, severity)
    line = raw.get("line")
    return Claim(
        text=text,
        kind=kind if kind in CLAIM_KINDS else default_kind,  # type: ignore[arg-type]
        severity=severity if severity in _SEVERITY_LEVEL else None,  # type: ignore[arg-type]
        file=str(raw["file"]).strip() or None if raw.get("file") else None,
        line=line if isinstance(line, int) and not isinstance(line, bool) and line > 0 else None,
        evidence=str(raw["evidence"]).strip() or None if raw.get("evidence") else None,
    )


def _number(claims: list[Claim]) -> list[Claim]:
    return [c.model_copy(update={"id": f"c{i}"}) for i, c in enumerate(claims, 1)]


def _unit(value: Any, default: float | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return min(1.0, max(0.0, float(value)))


_ITEM = re.compile(r"^\s*(?:\d+[.)]|[*\-•])\s+(.+)")
_LOCATION = re.compile(r"([\w./-]+\.[A-Za-z]{1,5}):(\d+)")


def _answer_from_prose(text: str, default_kind: ClaimKind) -> PanelAnswer:
    """Best effort for a model that ignored the schema: list items become claims."""
    claims: list[Claim] = []
    for line in text.splitlines():
        match = _ITEM.match(line)
        if not match:
            continue
        item = match.group(1).strip()
        where = _LOCATION.search(item)
        claims.append(
            Claim(
                text=item,
                kind=default_kind,
                file=where.group(1) if where else None,
                line=int(where.group(2)) if where else None,
            )
        )
    return PanelAnswer(summary=text.strip(), claims=_number(claims), confidence=0.5)


def parse_panel_answer(
    text: str, parsed_json: dict[str, Any] | None = None, *, task_key: str = "default"
) -> tuple[PanelAnswer, bool]:
    """A ``PanelAnswer`` from a model's reply, and whether the reply was valid claims JSON."""
    default_kind = _DEFAULT_KIND.get(task_key, "finding")
    data = parsed_json if parsed_json is not None else _extract_json(text)
    if isinstance(data, dict) and isinstance(data.get("claims"), list):
        claims = [c for c in (_clean_claim(r, default_kind) for r in data["claims"]) if c]
        patch = data.get("patch")
        answer = PanelAnswer(
            summary=str(data.get("summary") or "").strip(),
            claims=_number(claims),
            patch=patch if isinstance(patch, str) and patch.strip() else None,
            confidence=_unit(data.get("confidence"), 0.5) or 0.0,
            score=_unit(data.get("score"), None),
        )
        return answer, True
    return _answer_from_prose(text, default_kind), False


def render_panel_answer(answer: PanelAnswer) -> str:
    """A panelist's answer as Markdown, for display and for peers to read."""
    lines = [answer.summary.strip()] if answer.summary.strip() else []
    if lines and answer.claims:
        lines.append("")
    for claim in answer.claims:
        lines.append(f"- {_claim_line(claim)}")
    if answer.patch:
        lines.extend(["", *_fenced(answer.patch)])
    return "\n".join(lines)


def _fenced(patch: str) -> list[str]:
    """``patch`` in a code fence long enough that nothing inside it can close it."""
    longest = max((len(m) for m in re.findall(r"`+", patch)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}diff", patch.rstrip("\n"), fence]


def _claim_line(claim: Claim) -> str:
    where = ""
    if claim.file:
        where = f" ({claim.file}:{claim.line})" if claim.line else f" ({claim.file})"
    severity = f"[{claim.severity}] " if claim.severity else ""
    return f"{severity}{claim.kind}: {claim.text}{where}"


# ---------------------------------------------------------------------------------- clustering


def _tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9_]+", text.lower())
    stems = (re.sub(r"(?:ing|ed|es|s)$", "", w) if len(w) > 4 else w for w in words)
    return frozenset(s for s in stems if s and s not in _STOPWORDS)


def _similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _same_file(a: Claim, b: Claim) -> bool:
    return bool(a.file) and a.file == b.file


def _affinity(candidate: Claim, tokens: frozenset[str], member: ClusterMember) -> float:
    """How strongly ``candidate`` belongs with a cluster member; 0 when it does not."""
    other = member.claim
    if candidate.kind != other.kind:
        return 0.0
    near = (
        _same_file(candidate, other)
        and candidate.line is not None
        and other.line is not None
        and abs(candidate.line - other.line) <= LINE_WINDOW
    )
    similarity = _similarity(tokens, _tokens(other.text))
    if near:
        return 1.0 + similarity
    threshold = SIMILARITY_SAME_FILE if _same_file(candidate, other) else SIMILARITY
    return similarity if similarity >= threshold else 0.0


def _rank(claim: Claim) -> tuple[int, int, int]:
    level = _SEVERITY_LEVEL[claim.severity] if claim.severity else -1
    return (level, int(claim.has_evidence), len(claim.text))


def cluster_claims(answers: Mapping[str, PanelAnswer]) -> list[ClaimCluster]:
    """Group claims across models. Deterministic: models are taken in name order, claims in order.

    A cluster holds at most one claim per model, so its size is the number of models that agree.
    """
    groups: list[list[ClusterMember]] = []
    for model in sorted(answers):
        for claim in answers[model].claims:
            tokens = _tokens(claim.text)
            best: tuple[float, int] = (0.0, -1)
            for index, group in enumerate(groups):
                if any(member.model == model for member in group):
                    continue
                strength = max(_affinity(claim, tokens, member) for member in group)
                if strength > best[0]:
                    best = (strength, index)
            member = ClusterMember(model=model, claim=claim)
            if best[1] >= 0:
                groups[best[1]].append(member)
            else:
                groups.append([member])
    return [_build_cluster(i, group) for i, group in enumerate(groups, 1)]


def _build_cluster(number: int, group: list[ClusterMember]) -> ClaimCluster:
    lead = max(group, key=lambda m: _rank(m.claim)).claim
    levels = [_SEVERITY_LEVEL[m.claim.severity] for m in group if m.claim.severity]
    top = next((k for k, v in _SEVERITY_LEVEL.items() if levels and v == max(levels)), None)
    return ClaimCluster(
        id=f"C{number}",
        kind=lead.kind,
        text=lead.text,
        severity=top,  # type: ignore[arg-type]
        file=lead.file,
        line=lead.line,
        models=sorted({m.model for m in group}),
        members=group,
    )


# ---------------------------------------------------------------------------------- agreement


def agreement_score(
    clusters: list[ClaimCluster],
    n_models: int,
    *,
    n_requested: int | None = None,
    n_structured: int | None = None,
) -> AgreementReport:
    """Consensus, unique and contradicted sets, and a 0-1 agreement score for the clusters.

    ``score`` averages, over clusters, the share of the other models that back each one
    (``(support - 1) / (n - 1)``), halved for a cluster whose severities are split by two levels
    or more. It is 0 for fewer than two models: one model cannot agree with anyone.
    """
    requested = max(n_requested if n_requested is not None else n_models, n_models)
    structured = n_models if n_structured is None else n_structured
    majority = max(2, math.ceil(n_models / 2))
    consensus, unique, contradicted = [], [], []
    credit = 0.0
    for cluster in clusters:
        if cluster.support < 2:
            cluster.status = "unique"
            unique.append(cluster.id)
            continue
        if cluster.severity_spread >= 2:
            cluster.status = "contradicted"
            contradicted.append(cluster.id)
            credit += 0.5 * (cluster.support - 1) / max(n_models - 1, 1)
            continue
        if cluster.support >= majority:
            cluster.status = "consensus"
            consensus.append(cluster.id)
        else:
            cluster.status = "partial"
        credit += (cluster.support - 1) / max(n_models - 1, 1)
    low_information = n_models < 2
    claims = [m.claim for c in clusters for m in c.members]
    report = AgreementReport(
        n_models=n_models,
        n_requested=requested,
        n_clusters=len(clusters),
        score=0.0 if low_information or not clusters else credit / len(clusters),
        consensus=consensus,
        unique=unique,
        contradicted=contradicted,
        outliers=_outliers(clusters, n_models),
        evidence_rate=sum(c.has_evidence for c in claims) / len(claims) if claims else 0.0,
        coverage=n_models / requested if requested else 0.0,
        structured_share=structured / n_models if n_models else 0.0,
        low_information=low_information,
    )
    report.confidence = calibrated_confidence(report)
    return report


def _outliers(clusters: list[ClaimCluster], n_models: int) -> list[str]:
    """Models whose every claim stands alone while the others share some (needs 3+ models)."""
    if n_models < 3 or not any(c.support >= 2 for c in clusters):
        return []
    models = {m for c in clusters for m in c.models}
    shared = {m for c in clusters if c.support >= 2 for m in c.models}
    return sorted(models - shared)


def calibrated_confidence(report: AgreementReport) -> float:
    """Confidence from agreement, evidence and coverage; never from how an answer is worded.

    ``0.5 * agreement + 0.3 * evidence_rate + 0.2 * (coverage * structured_share)``, minus
    ``0.15`` times the share of contradicted clusters, capped at 0.5 when fewer than two models
    answered (there is no agreement to measure) and clamped to [0, 0.95].
    """
    if report.n_models == 0:
        return 0.0
    answered = report.coverage * report.structured_share
    raw = W_AGREEMENT * report.score + W_EVIDENCE * report.evidence_rate + W_COVERAGE * answered
    if report.contradicted:
        raw -= CONTRADICTION_PENALTY * len(report.contradicted) / max(report.n_clusters, 1)
    if report.low_information:
        raw = min(raw, LOW_INFORMATION_CAP)
    return round(min(max(raw, 0.0), 0.95), 4)


def cluster_line(cluster: ClaimCluster) -> str:
    """One display line for a cluster: severity, text, location and which models back it."""
    where = ""
    if cluster.file:
        where = f" ({cluster.file}:{cluster.line})" if cluster.line else f" ({cluster.file})"
    severity = f"[{cluster.severity}] " if cluster.severity else ""
    return f"{severity}{cluster.text}{where}, {cluster.kind}, {', '.join(cluster.models)}"


def top_clusters(clusters: Iterable[ClaimCluster], limit: int = 5) -> list[ClaimCluster]:
    """Most important first: severity, then how many models back it."""

    def key(cluster: ClaimCluster) -> tuple[int, int, str]:
        level = _SEVERITY_LEVEL[cluster.severity] if cluster.severity else -1
        return (-level, -cluster.support, cluster.id)

    return sorted(clusters, key=key)[:limit]
