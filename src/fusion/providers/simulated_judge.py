"""How a simulated model plays the benchmark judge (``role == "bench_judge"``).

A judge call carries the data it is about in ``request.metadata["judge"]``, as ``kind`` and
``payload`` (real providers ignore it; the payload is what the prompt shows, never ground
truth). The
simulated judge reads the payload and decides by word overlap, the way a middling judge would, then
errs in proportion to ``1 - skill``. Pairwise it also knows the task's truth (through the world)
so that a better answer really is better, leans towards answer A by ``position_bias``, and
calls a tie when the two are close. Every draw is a hash of the seed, the model and the inputs:
reruns are identical.
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
