"""Drafting candidate tasks with a model, on a spend cap, for a person to review.

Nothing generated goes into a dataset by itself: ``generate_candidates`` returns authoring items
that compile and are written to a file marked unreviewed. The compiler rejects malformed drafts
(missing bug markers, bad schema); whether the seeded defects are *real*, the rubric fair and the
code realistic is for the reviewer, which is why the dataset README calls its tasks synthetic.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from fusion.bench.datasets.build import AuthoringError, category_of, compile_item
from fusion.bench.runner import BenchEnv
from fusion.bench.spec import BenchTask
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.providers.base import ModelRequest
from fusion.routing.budget import PlannedCall, forecast_calls

__all__ = ["Generated", "estimate_generation", "generate_candidates", "run_generation"]

SPEND_TASK = "dataset-build"
LANGUAGES = ("python", "typescript", "go")
DIFFICULTIES = ("easy", "medium", "hard", "medium")
_DRAFT_TOKENS = 5000  # planning assumption: the length of one drafted task

_SHAPES = {
    "code_review": (
        "A code-review task. JSON keys: title, description (the pull request text), files "
        "(a list of {path, diff}), bugs (a list of {id, category, severity, description, "
        "aliases}).\n"
        "Every diff line starts with '=' (unchanged), '+' (added) or '-' (removed). Mark each "
        "seeded bug by writing «ID» at the end of the added line it is on, using the bug's id. "
        "The diff must hold 30-120 lines of realistic code. Seed exactly the listed defects "
        "and no other defect, so that a reviewer who finds anything else is wrong. If the "
        "task is clean, give an empty bugs list and no markers."
    ),
    "debugging": (
        "A debugging task. JSON keys: symptom, files (a list of {path, content}), trace, logs, "
        "root_cause_tags (equivalent canonical names for the one root cause), root_cause_aliases "
        "(tag -> other wordings), root_cause (one sentence), fix_keywords (elements of the fix; "
        "'a|b' accepts either). The code, trace and logs must agree and total 30-200 lines."
    ),
    "architecture": (
        "An architecture-decision task. JSON keys: prompt, context, required (a list of "
        "{text, keywords, gate}), forbidden (a list of {text, keywords}). Required points are "
        "concepts a good answer must cover, with keywords listing alternative wordings; mark the "
        "one or two that decide correctness with gate. More than one design may be acceptable: "
        "do not require a particular product unless the constraints force it."
    ),
    "planning": (
        "An implementation-planning task. JSON keys: prompt, context (the existing codebase or "
        "constraints), required (a list of {text, keywords, gate}), forbidden (a list of "
        "{text, keywords}). Required points are steps or risks a sound plan must cover, with "
        "keywords listing alternative wordings."
    ),
}


@dataclass
class Generated:
    items: list[dict[str, Any]] = field(default_factory=list)  # authoring items that compile
    rejected: list[str] = field(default_factory=list)  # why each rejected draft was
    cost_usd: float = 0.0


def _prompt(category: str, n: int, language: str, difficulty: str, topic: str) -> str:
    where = f" in {language}" if category in ("code_review", "debugging") else ""
    return (
        f"Write one new {difficulty} benchmark task{where} for the category '{category}'. "
        f"Topic hint: {topic}. {_SHAPES[category]}\n\n"
        "The task must be self-contained, realistic, and original; contain no real secrets; and "
        "have unambiguous ground truth. Answer with one JSON object only."
    )


def estimate_generation(env: BenchEnv, alias: str, count: int) -> float:
    """What drafting ``count`` tasks is expected to cost at the catalog's prices."""
    call = PlannedCall("eval", alias, 900, _DRAFT_TOKENS)
    return forecast_calls([call] * count, env.registry.models, env.pricing).usd


async def generate_candidates(
    category: str,
    count: int,
    *,
    gateway: CallGateway,
    alias: str,
    topics: list[str] | None = None,
    existing: list[BenchTask] | None = None,
) -> Generated:
    """Draft ``count`` tasks of ``category`` with the model ``alias``; drafts that do not compile
    or repeat an existing prompt are reported in ``rejected``."""
    category_of(category)
    entry = gateway.models[alias]
    hints = topics or ["a small web service", "a data-processing job", "a CLI tool", "a library"]
    seen = {t.prompt for t in existing or []}
    spent = gateway.ledger.total_cost().usd
    out = Generated()

    async def draft(n: int) -> dict[str, Any] | str:
        request = ModelRequest(
            model_id=entry.model_id,
            system_prompt="You author benchmark tasks for evaluating code assistants. "
            "Answer with JSON only.",
            user_prompt=_prompt(
                category,
                n,
                LANGUAGES[n % len(LANGUAGES)],
                DIFFICULTIES[n % len(DIFFICULTIES)],
                hints[n % len(hints)],
            ),
            max_tokens=_DRAFT_TOKENS + 1000,
            json_mode=entry.supports_json,
            metadata={"role": "dataset_builder"},
        )
        response = await gateway.call(stage="eval", alias=alias, request=request)
        if response.error:
            return f"draft {n}: the call failed: {response.error}"
        data = response.parsed_json
        if data is None:
            start, end = response.text.find("{"), response.text.rfind("}") + 1
            try:
                data = json.loads(response.text[start:end])
            except json.JSONDecodeError:
                return f"draft {n}: not valid JSON"
        return data if isinstance(data, dict) else f"draft {n}: not a JSON object"

    for n, result in enumerate(await asyncio.gather(*(draft(i) for i in range(count)))):
        if isinstance(result, str):
            out.rejected.append(result)
            continue
        item = {
            **result,
            "id": f"gen-{category}-{n + 1:03d}",
            "category": category,
            "language": result.get("language", LANGUAGES[n % len(LANGUAGES)]),
            "difficulty": DIFFICULTIES[n % len(DIFFICULTIES)],
            "tags": [*result.get("tags", []), "unreviewed"],
        }
        try:
            task = compile_item(item)
        except AuthoringError as exc:
            out.rejected.append(f"draft {n + 1}: {exc}")
            continue
        if task.prompt in seen:
            out.rejected.append(f"draft {n + 1}: repeats an existing prompt")
            continue
        seen.add(task.prompt)
        out.items.append(item)
    out.cost_usd = gateway.ledger.total_cost().usd - spent
    return out


async def run_generation(
    env: BenchEnv,
    category: str,
    count: int,
    *,
    alias: str,
    max_usd: float,
    topics: list[str] | None = None,
    existing: list[BenchTask] | None = None,
) -> Generated:
    """``generate_candidates`` under the study rules: forecast first, refuse over ``max_usd`` or
    the live-spend cap, and record what was actually spent."""
    if alias not in env.registry.models:
        msg = f"model '{alias}' is not in the catalog"
        raise AuthoringError(msg)
    estimate = estimate_generation(env, alias, count)
    if estimate > max_usd:
        msg = (
            f"drafting {count} tasks is forecast at ${estimate:.4f}, over --max-usd ${max_usd:.2f}"
        )
        raise AuthoringError(msg)
    if env.spend is not None:
        env.spend.check(estimate, "dataset generation")
    ledger = RunLedger(asyncio.get_running_loop().time)
    gateway = CallGateway(
        ledger=ledger,
        models=env.registry.models,
        providers=env.providers,
        pricing=env.pricing,
        truncate_prompts=False,
        temperature=0.7,
        redact=False,  # drafted tasks may contain planted secrets on purpose
    )
    try:
        return await generate_candidates(
            category, count, gateway=gateway, alias=alias, topics=topics, existing=existing
        )
    finally:
        if env.spend is not None:
            env.spend.append(
                ledger.total_cost().usd, task=SPEND_TASK, purpose=f"{count} {category} drafts"
            )
