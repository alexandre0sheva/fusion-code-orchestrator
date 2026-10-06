"""How a simulated model plays the benchmark judge (``role == "bench_judge"``).

A judge call carries the data it is about in ``request.metadata["judge"]``, as ``kind`` and
``payload`` (real providers ignore it; the payload is what the prompt shows, never ground truth).
The simulated judge reads the payload and decides by word overlap, the way a middling judge would,
then errs in proportion to ``1 - skill``. Pairwise it also knows the task's truth (through the
world) so that a better answer really is better, leans towards answer A by ``position_bias``, and
calls a tie when the two are close. The agentic judge (``kind == "agentic"``) plays the tool
protocol of ``fusion.bench.scoring.agentic``: it fetches the evidence of each output, then submits
the measured scores plus noise. Every draw is a hash of the seed, the model and the inputs: reruns
are identical.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fusion.bench.spec import BenchTask
    from fusion.providers.simulated import SimModel, SimWorld

__all__ = ["JUDGE_ROLE", "judge_reply"]

JUDGE_ROLE = "bench_judge"
_WORD = re.compile(r"[a-z0-9]{4,}")
_TIE_MARGIN = 0.1
_PAIR_NOISE = 1.0  # spread of a judge's error in a pairwise score, at skill 0


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _overlap(needle: str, haystack: str) -> float:
    """The share of ``needle``'s words that ``haystack`` has."""
    words = _words(needle)
    return len(words & _words(haystack)) / len(words) if words else 0.0


def _digest(*texts: str) -> str:
    return hashlib.sha256("\x00".join(texts).encode()).hexdigest()[:12]


def judge_reply(
    judge: Any, *, model: str, spec: SimModel, world: SimWorld, seed: int | None
) -> str | None:
    """The JSON a simulated judge answers with, or None for a call it does not understand."""
    if not isinstance(judge, dict) or not isinstance(judge.get("payload"), dict):
        return None
    payload: dict[str, Any] = judge["payload"]
    kind = judge.get("kind")
    if kind == "match":
        return json.dumps(_match(payload, model, spec, world, seed))
    if kind == "equivalence":
        return json.dumps(_equivalence(payload, model, spec, world, seed))
    if kind == "rubric":
        return json.dumps(_rubric(payload, model, spec, world, seed))
    if kind == "pairwise":
        return json.dumps(_pairwise(payload, model, spec, world, seed))
    if kind == "agentic":
        return json.dumps(_agentic(payload, model, spec, world, seed))
    return None


def _match(
    payload: dict[str, Any], model: str, spec: SimModel, world: SimWorld, seed: int | None
) -> dict[str, Any]:
    used: set[Any] = set()
    matches = []
    for bug in payload.get("bugs", []):
        best, score = None, 0.0
        for finding in payload.get("findings", []):
            if finding["id"] in used:
                continue
            s = _overlap(bug["description"], finding["text"])
            if s > score:
                best, score = finding, s
        missed = world.uniform("jmatch", model, bug["id"], seed) < (1.0 - spec.skill) * 0.3
        if best is not None and score >= 0.4 and not missed:
            used.add(best["id"])
            matches.append({"defect": bug["id"], "finding": best["id"], "quote": best["text"][:80]})
        else:
            matches.append({"defect": bug["id"], "finding": None, "quote": ""})
    return {"matches": matches}


def _equivalence(
    payload: dict[str, Any], model: str, spec: SimModel, world: SimWorld, seed: int | None
) -> dict[str, Any]:
    cause = str(payload.get("cause", ""))
    said = []
    for cand in payload.get("candidates", []):
        hit = _overlap(cause, cand["text"]) >= 0.4
        flip = world.uniform("jequiv", model, cand["id"], seed) < (1.0 - spec.skill) * 0.15
        if hit != flip:
            said.append(cand["id"])
    return {"equivalent": said}


def _rubric(
    payload: dict[str, Any], model: str, spec: SimModel, world: SimWorld, seed: int | None
) -> dict[str, Any]:
    answer = str(payload.get("answer", ""))
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", answer) if len(s.strip()) >= 8]
    rows = []
    for item in payload.get("items", []):
        best, score = "", 0.0
        for sentence in sentences:
            s = _overlap(item["text"], sentence)
            if s > score:
                best, score = sentence, s
        met = score >= 0.5
        if world.uniform("jrubric", model, item["id"], seed) < (1.0 - spec.skill) * 0.1:
            met = not met  # a slip: a miss, or credit for the nearest unrelated sentence
            best = best or (sentences[0] if sentences else "")
        rows.append({"id": item["id"], "met": met and bool(best), "quote": best if met else ""})
    return {"items": rows}


def _quality(task: BenchTask, answer: str) -> float:
    """How well an answer says what the task's truth says, by word overlap: the oracle a
    simulated judge perceives quality through."""
    truth = task.truth
    good: list[str] = [
        p.get("text") or p["keywords"][0] for p in truth.get("points", []) if isinstance(p, dict)
    ]
    bad: list[str] = [
        p.get("text") or p["keywords"][0] for p in truth.get("decoys", []) if isinstance(p, dict)
    ]
    good += [b["description"] for b in truth.get("bugs", [])]
    if truth.get("root_cause_tags"):
        good.append(truth.get("root_cause") or truth["root_cause_tags"][0])
        good += [k.split("|")[0] for k in truth.get("fix_keywords", [])]
    for key, bucket in (("required_points", good), ("forbidden_points", bad)):
        for item in truth.get(key, []):
            bucket.append(item if isinstance(item, str) else item["text"])
    if not good:
        return 0.0
    found = sum(_overlap(g, answer) >= 0.6 for g in good) / len(good)
    asserted = sum(_overlap(b, answer) >= 0.6 for b in bad) / len(bad) if bad else 0.0
    return found - 0.5 * asserted


def _pairwise(
    payload: dict[str, Any], model: str, spec: SimModel, world: SimWorld, seed: int | None
) -> dict[str, Any]:
    a, b = str(payload.get("a", "")), str(payload.get("b", ""))
    task = world.task_for(str(payload.get("task", "")))
    if task is None:
        return {"winner": "tie", "reason": "unknown task"}
    noise = world.normal("jpair", model, seed, _digest(a), _digest(b)) * (1.0 - spec.skill)
    margin = _quality(task, a) - _quality(task, b) + _PAIR_NOISE * noise + spec.position_bias
    winner = "tie" if abs(margin) < _TIE_MARGIN else "A" if margin > 0 else "B"
    return {"winner": winner, "reason": "simulated judgement"}


_EVIDENCE_ORDER = ("tests", "perf", "a11y", "screenshot", "static")
_FETCHES_PER_OUTPUT = 4
_AGENTIC_NOISE = 0.12  # spread of a score's error at skill 0
_AGENTIC_TIE = 0.05


def _agentic(
    payload: dict[str, Any], model: str, spec: SimModel, world: SimWorld, seed: int | None
) -> dict[str, Any]:
    """One turn of the agentic judge: fetch the next piece of evidence, or submit the verdict.

    It reads only what the conversation shows (the evidence it fetched), like a real judge: it
    scores each criterion with the measured score plus noise that shrinks with skill, and a
    criterion nothing measures with the average of the ones that are measured.
    """
    labels: list[str] = payload["labels"]
    observed: list[dict[str, Any]] = payload["observations"]
    index: list[dict[str, str]] = payload["evidence_index"]
    step, cap = int(payload["step"]), int(payload["max_steps"])
    fetched = {
        (o["args"].get("label"), o["args"].get("kind"))
        for o in observed
        if o["tool"] == "get_evidence" and not o.get("error")
    }
    if step < cap:
        for label in labels:
            kinds = [
                k
                for k in _EVIDENCE_ORDER
                if any(e["label"] == label and e["kind"] == k for e in index)
            ]
            for kind in kinds[:_FETCHES_PER_OUTPUT]:
                if (label, kind) not in fetched:
                    return {"tool": "get_evidence", "args": {"label": label, "kind": kind}}
    return _agentic_verdict(payload, model, spec, world, seed)


def _agentic_verdict(
    payload: dict[str, Any], model: str, spec: SimModel, world: SimWorld, seed: int | None
) -> dict[str, Any]:
    from fusion.bench.evaluators.base import Evidence
    from fusion.bench.scoring.completion import by_kind, measured_score
    from fusion.bench.spec import Criterion

    labels: list[str] = payload["labels"]
    criteria = [Criterion.model_validate(c) for c in payload["criteria"]]
    seen: dict[str, list[Evidence]] = {label: [] for label in labels}
    for o in payload["observations"]:
        if o["tool"] in ("get_evidence", "run_evaluator") and not o.get("error"):
            label = o["args"].get("label")
            for dump in o.get("evidence", []):
                if label in seen:
                    seen[label] = [e for e in seen[label] if e.id != dump["id"]]
                    seen[label].append(Evidence.model_validate(dump))
    spread = _AGENTIC_NOISE * (1.0 - spec.skill)
    scores: dict[str, dict[str, float]] = {}
    for label in labels:
        kinds = by_kind(seen[label])
        measured = {c.id: measured_score(c, kinds) for c in criteria}
        known = [v for v in measured.values() if v is not None]
        proxy = sum(known) / len(known) if known else 0.5
        scores[label] = {}
        for c in criteria:
            value = measured[c.id]
            base = proxy if value is None else value
            noise = (
                world.normal("jagentic", model, seed, label, c.id, payload["task"][:40]) * spread
            )
            scores[label][c.id] = round(min(max(base + noise, 0.0), 1.0), 3)
    cited = sorted({e.id for items in seen.values() for e in items})
    text = "Judged from the evidence" + (f" {', '.join(cited)}" if cited else "") + "."
    args: dict[str, Any] = {
        "scores": scores if payload["mode"] == "pairwise" else scores[labels[0]],
        "justification": text + " The outputs were compared on the measured results.",
        "evidence": cited,
    }
    if payload["mode"] == "pairwise":
        weights = {c.id: c.weight for c in criteria}

        def total(label: str) -> float:
            return sum(weights[k] * v for k, v in scores[label].items()) / sum(weights.values())

        margin = total("A") - total("B") + spec.position_bias
        args["winner"] = "tie" if abs(margin) < _AGENTIC_TIE else "A" if margin > 0 else "B"
    return {"tool": "submit_verdict", "args": args}
