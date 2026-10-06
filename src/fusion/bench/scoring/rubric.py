"""``RubricScorer``: a checklist of required and forbidden points for architecture and planning.

Each required item is judged met or not by an LLM judge, which must quote the part of the answer
that meets it; a quote that is in neither the answer nor the evidence voids the verdict, so a
judge cannot award a point it invented. With several ``judge_models`` each item goes by
majority. An item with
``keywords`` can be decided without a judge (any keyword in the answer), which is what happens
when the study names none. Items the scorer could not decide either way are left out of the
fraction and listed in ``details["undecided"]``; with none decidable it raises ``ScoringError``
rather than guess.

``quality`` is the weighted fraction of required items met, less 0.5 times the share of forbidden
items the answer asserts. A missed ``gate`` item caps it at ``GATE_CAP``, below the pass mark.
Measured ``Evidence`` (tests, timings) is shown to the judge next to the answer.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fusion.bench.scoring.base import (
    AnswerView,
    Evidence,
    ScoreEnv,
    ScoreResult,
    ScoringError,
    ask_judge,
    estimate_judge_call,
    normalize,
    task_text,
)
from fusion.bench.scoring.points import points_fallback
from fusion.bench.spec import BenchTask, RubricItem, RubricTruth
from fusion.routing.budget import PlannedCall
from fusion.security.untrusted import wrap_untrusted

__all__ = ["GATE_CAP", "RubricScorer"]

GATE_CAP = 0.4
FORBIDDEN_PENALTY = 0.5
_ITEMS_PER_ITEM_TOKENS = 60


def _keyword_verdict(item: RubricItem, answer: str) -> bool | None:
    if not item.keywords:
        return None
    text = answer.lower()
    return any(k.lower() in text for k in item.keywords)


def _quoted(quote: Any, answer: str) -> bool:
    return (
        isinstance(quote, str) and len(quote.strip()) >= 8 and normalize(quote) in normalize(answer)
    )


class RubricScorer:
    """Checklist fraction with gating items; see the module doc."""

    name = "rubric"

    def estimate_calls(self, task: BenchTask, judges: list[str]) -> list[PlannedCall]:
        if not {"required_points", "forbidden_points"} & task.truth.keys():
            return []
        truth = RubricTruth.model_validate(task.truth)
        n = len(truth.required_points) + len(truth.forbidden_points)
        extra = n * _ITEMS_PER_ITEM_TOKENS
        return [estimate_judge_call(task, j, extra_tokens=extra) for j in judges]

    async def score(
        self,
        task: BenchTask,
        answer: AnswerView,
        env: ScoreEnv,
        evidence: Evidence | None = None,
    ) -> ScoreResult:
        if not {"required_points", "forbidden_points"} & task.truth.keys():
            return await points_fallback(task, self.name).score(task, answer, env, evidence)
        truth = RubricTruth.model_validate(task.truth)
        spent = env.spent_usd()
        items = [*truth.required_points, *truth.forbidden_points]
        verdicts = await self._judge(task, items, answer.text(), env, evidence)
        met: dict[str, bool] = {}
        by: dict[str, str] = {}
        for item in items:
            if item.id in verdicts:
                met[item.id], by[item.id] = verdicts[item.id], "judge"
            elif (kw := _keyword_verdict(item, answer.text())) is not None:
                met[item.id], by[item.id] = kw, "keywords"
        required = [i for i in truth.required_points if i.id in met]
        forbidden = [i for i in truth.forbidden_points if i.id in met]
        undecided = [i.id for i in items if i.id not in met]
        if truth.required_points and not required:
            msg = (
                f"task '{task.id}': none of its rubric items could be decided; name "
                "--judge-models, or give its items keywords"
            )
            raise ScoringError(msg)
        total = sum(i.weight for i in required)
        fraction = sum(i.weight for i in required if met[i.id]) / total if total else 1.0
        asserted = [i.id for i in forbidden if met[i.id]]
        penalty = FORBIDDEN_PENALTY * len(asserted) / len(forbidden) if forbidden else 0.0
        quality = max(fraction - penalty, 0.0)
        gates_missed = [i.id for i in required if i.gate and not met[i.id]]
        if gates_missed:
            quality = min(quality, GATE_CAP)
        details: dict[str, Any] = {
            "met": [i.id for i in required if met[i.id]],
            "missed": [i.id for i in required if not met[i.id]],
            "gates_missed": gates_missed,
            "forbidden_asserted": asserted,
            "undecided": undecided,
            "decided_by": by,
            "fraction": fraction,
        }
        if env.errors:
            details["judge_errors"] = list(env.errors)
        return ScoreResult(
            quality=min(quality, 1.0),
            scorer=self.name,
            details=details,
            cost_usd=env.spent_usd() - spent,
        )

    async def _judge(
        self,
        task: BenchTask,
        items: list[RubricItem],
        answer: str,
        env: ScoreEnv,
        evidence: Evidence | None,
    ) -> dict[str, bool]:
        """Majority verdict per item over the judge models; empty with no judge or no answer."""
        if not env.judge_models or not items:
            return {}
        checklist = [{"id": i.id, "text": i.text} for i in items]
        listing = "\n".join(f"{c['id']}: {c['text']}" for c in checklist)
        measured = (
            f"\n\nMeasured evidence about the answer:\n{evidence.render()}" if evidence else ""
        )
        prompt = (
            "Judge whether the answer below meets each checklist item. An item is met only if "
            "the answer actually says it (or the evidence shows it); quote the exact words of the "
            "answer that meet it. Do not credit what the answer merely implies.\n\n"
            f"Task:\n{task_text(task, files=False)}\n\n"
            f"Answer:\n{wrap_untrusted(answer)}{measured}\n\n"
            f"Checklist:\n{listing}\n\n"
            'Answer: {"items": [{"id": "r1", "met": true, "quote": "..."}]}'
        )
        payload = {"answer": answer, "items": checklist}
        replies = await asyncio.gather(
            *(
                ask_judge(
                    env, alias, kind="rubric", prompt=prompt, payload=payload, max_tokens=2000
                )
                for alias in env.judge_models
            )
        )
        votes: dict[str, list[bool]] = {}
        for reply in replies:
            if reply is None:
                continue
            for row in reply.get("items", []):
                if not isinstance(row, dict) or row.get("id") not in {i.id for i in items}:
                    continue
                quote = row.get("quote")
                said = bool(row.get("met")) and (
                    _quoted(quote, answer)
                    or (evidence is not None and _quoted(quote, evidence.render()))
                )
                votes.setdefault(str(row["id"]), []).append(said)
        return {k: sum(v) * 2 > len(v) for k, v in votes.items()}
