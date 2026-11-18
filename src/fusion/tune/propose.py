"""Propose: a cheap model suggests one edit to one prompt of the pack, and the code checks it.

An edit appends to a prompt or replaces it. Before an edit is probed it must pass
``validate_edits``: the prompt exists, every ``{{placeholder}}`` the code fills is still there,
the pack stays within its size budget, and no line of grader material (truth points, hidden tests,
reference solutions, rubrics) appears in it. The proposer and composer prompts carry the pack and
per-category scores only, never a task, and are checked against the same material.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fusion.bench.spec import BenchTask
from fusion.config.catalog import ModelEntry
from fusion.config.prompt_pack import PROMPT_SPECS, Axis, PromptPack, pack_errors
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.providers.base import ModelProvider, ModelRequest
from fusion.providers.simulated_tune import TUNE_COMPOSER_ROLE, TUNE_PROPOSER_ROLE
from fusion.security.untrusted import UNTRUSTED_RULES, wrap_untrusted
from fusion.telemetry.cost import PricingRegistry
from fusion.verify.synthesis import EvidenceLeakError, assert_no_grader_material, grader_material

__all__ = [
    "AXIS_TEXT",
    "Edit",
    "ModelTuner",
    "Tuner",
    "TunerReply",
    "compose_prompt",
    "leaks",
    "parse_composition",
    "parse_proposal",
    "propose_prompt",
    "tune_material",
    "validate_edits",
]

TUNER_OUTPUT_TOKENS = 1200
LEAK_ERROR = "the edit contains grader material"
_MIN_MATERIAL_CHARS = 16

AXIS_TEXT: Mapping[str, str] = {
    "inv": "INV (procedure): the shape of the answer and the verification steps an answerer "
    "takes before it replies, the same for every task type",
    "dom": "DOM (domain knowledge): what to look for in one task type (code review, debugging, "
    "architecture, planning, coding answers) and the mistakes typical of it",
}


class Edit(BaseModel):
    """One change to one prompt: ``append`` adds a paragraph, ``replace`` gives the whole text."""

    model_config = ConfigDict(extra="ignore")

    target: str
    op: Literal["append", "replace"] = "append"
    text: str = Field(min_length=1)
    rationale: str = ""

    def apply(self, pack: PromptPack) -> PromptPack:
        current = pack.text(self.target)
        text = self.text.strip("\n")
        new = f"{current}\n{text}" if self.op == "append" else text
        return pack.with_edits({self.target: new}, origin="tune")

    def summary(self, width: int = 100) -> str:
        flat = re.sub(r"\s+", " ", self.text).strip()
        head = f"{self.op} {self.target}: "
        room = max(width - len(head), 10)
        return head + (flat if len(flat) <= room else flat[: room - 1] + "…")


# -------------------------------------------------------------------------------- validation


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield from value.splitlines()
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def tune_material(tasks: Sequence[BenchTask]) -> dict[str, list[str]]:
    """Every line that grades a task (all splits), minus what the model is shown anyway.

    Task 10's ``grader_material`` (hidden tests, reference solutions, rubrics) plus every line of
    each task's ground truth (review points, root causes, decoys) that is long enough to be telling.
    """
    material: dict[str, list[str]] = {}
    for task in tasks:
        seen = "\n".join([task.prompt, task.context, *task.files.values()])
        for source, lines in grader_material(task.truth, task.files).items():
            material.setdefault(source, []).extend(lines)
        truth = [
            line.strip()
            for line in _strings(task.truth)
            if len(line.strip()) >= _MIN_MATERIAL_CHARS and line.strip() not in seen
        ]
        if truth:
            material.setdefault("ground truth", []).extend(truth)
    return {source: sorted(set(lines)) for source, lines in material.items()}


def validate_edits(
    base: PromptPack,
    edits: Sequence[Edit],
    *,
    material: Mapping[str, list[str]],
    reference: PromptPack | None = None,
    max_prompt_chars: int,
    max_growth_chars: int,
) -> tuple[PromptPack | None, list[str]]:
    """The pack ``edits`` make from ``base``, or None and the reasons it may not be probed.

    ``reference`` is the pack the size budget is measured against (the untuned base).
    """
    errors: list[str] = []
    if not edits:
        return None, ["no edit was proposed"]
    pack = base
    for edit in edits:
        if edit.target not in PROMPT_SPECS:
            errors.append(f"'{edit.target}' is not a prompt of the pack")
            continue
        if not edit.text.strip():
            errors.append(f"the edit to '{edit.target}' is empty")
            continue
        pack = edit.apply(pack)
    if errors:
        return None, errors
    changed = base.changed(pack)
    if not changed:
        return None, ["the edit changes nothing"]
    errors.extend(pack_errors(pack))
    ref = reference or base
    for name in changed:
        if len(pack.text(name)) > max_prompt_chars:
            errors.append(
                f"prompt '{name}' would be {len(pack.text(name))} characters; the size budget "
                f"is {max_prompt_chars}"
            )
    growth = sum(len(t) for t in pack.texts.values()) - sum(len(t) for t in ref.texts.values())
    if growth > max_growth_chars:
        errors.append(
            f"the pack would grow by {growth} characters; the size budget is {max_growth_chars}"
        )
    try:
        assert_no_grader_material("\n".join(pack.text(n) for n in changed), material)
    except EvidenceLeakError as exc:  # the message would quote the material: name its source only
        source = re.search(r"\(([^)]*)\)", str(exc))
        errors.append(f"{LEAK_ERROR} ({source.group(1) if source else 'unknown source'})")
    return (None, errors) if errors else (pack, [])


def leaks(errors: Sequence[str]) -> bool:
    """Did validation find grader material? Such an edit is never shown again, to anyone."""
    return any(e.startswith(LEAK_ERROR) for e in errors)


# ----------------------------------------------------------------------------------- parsing


def _json_object(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_proposal(text: str) -> tuple[Edit | None, str | None]:
    """A proposer's edit, or None and why it could not be read."""
    data = _json_object(text)
    if data is None:
        return None, "the proposer did not return a JSON object"
    try:
        return Edit.model_validate(data), None
    except ValidationError as exc:
        return None, f"the proposal is not a valid edit: {exc.errors()[0]['msg']}"


def parse_composition(text: str) -> tuple[list[Edit], str | None]:
    """A composer's edits (possibly none), or an empty list and why they could not be read."""
    data = _json_object(text)
    if data is None or not isinstance(data.get("edits"), list):
        return [], "the composer did not return {\"edits\": [...]}"
    edits: list[Edit] = []
    for raw in data["edits"]:
        try:
            edits.append(Edit.model_validate(raw))
        except ValidationError as exc:
            return [], f"a composed edit is not valid: {exc.errors()[0]['msg']}"
    return edits, None


# ----------------------------------------------------------------------------------- prompts

PROPOSER_SYSTEM = f"""\
You improve the prompts of Fusion, a tool that asks a panel of cheap models to review code, \
debug, plan, decide architecture and answer coding questions. Its prompts are a pack of short \
Markdown texts. Propose ONE edit to ONE prompt, along the axis named below, that would make the \
panel's answers score higher, above all on the task types that score low.

Rules:
1. Edit only a prompt listed under Targets. op "append" adds a paragraph at the end of the \
prompt; op "replace" gives the prompt's complete new text.
2. Keep every {{{{placeholder}}}} a prompt has, unchanged: the code fills them in.
3. Be general: the edit must help on tasks nobody has seen. Never mention a particular task, \
file, identifier or expected answer.
4. Be short: one to three sentences for an append.
5. Do not repeat an edit already proposed (listed below); try a different idea.
{UNTRUSTED_RULES}
Reply with JSON only: {{"target": "<prompt name>", "op": "append" or "replace", "text": "...", \
"rationale": "one sentence"}}"""

COMPOSER_SYSTEM = f"""\
You combine measured prompt edits into one final version of Fusion's prompt pack. Each edit was \
tried alone against the unedited pack on the same tasks; its effect on answer quality is given \
with a noise band and by task type.

Rules:
1. Keep edits whose verdict is "helped". Leave out "hurt" and "no_effect" edits.
2. An edit marked "confounded" helped some task types and hurt others: keep it only narrowed to \
the task types it helped, for example by moving its text into that task type's own prompt.
3. Edits to the same prompt may be merged into one text; keep every {{{{placeholder}}}}.
4. Never add a particular task, file, identifier or expected answer.
{UNTRUSTED_RULES}
Reply with JSON only: {{"edits": [{{"target": "<prompt name>", "op": "append" or "replace", \
"text": "..."}}], "rationale": "one sentence"}}"""


def _score_table(scores: Mapping[str, tuple[int, float, float]]) -> str:
    lines = ["| task type | tasks | mean quality | solved |", "|---|---|---|---|"]
    for category, (n, quality, solved) in sorted(scores.items()):
        lines.append(f"| {category} | {n} | {quality:.3f} | {solved:.0%} |")
    return "\n".join(lines)


def propose_prompt(
    *,
    pack: PromptPack,
    axis: Axis,
    scores: Mapping[str, tuple[int, float, float]],
    earlier: Sequence[str],
    index: int,
    of: int,
) -> str:
    """The proposer's user prompt: the axis, the target prompts as they are, the dev scores by
    task type (``(tasks, mean quality, solved share)``), and the edits proposed before."""
    targets = [name for name, spec in PROMPT_SPECS.items() if spec.axis == axis]
    parts = [f"## Axis\n{AXIS_TEXT[axis]}", "\n## Targets"]
    parts += [f"- {name}: {PROMPT_SPECS[name].summary}" for name in targets]
    parts.append("\n## The target prompts as they are")
    for name in targets:
        parts.append(f"\n### {name}\n{wrap_untrusted(pack.text(name))}")
    parts.append(f"\n## How the current pack scores on the tuning tasks\n{_score_table(scores)}")
    parts.append("\n## Already proposed")
    parts += [f"- {line}" for line in earlier] or ["(nothing yet)"]
    parts.append(f"\nYou are proposer {index + 1} of {of}.")
    return "\n".join(parts)


def compose_prompt(*, pack: PromptPack, contrasts: Sequence[str], index: int, of: int) -> str:
    """The composer's user prompt: each measured edit with its effect (the behavioural
    contrasts), and the current text of every prompt they touch."""
    parts = ["## Measured edits", *contrasts]
    touched = sorted({m.group(1) for c in contrasts if (m := re.search(r"prompt `([^`]+)`", c))})
    if touched:
        parts.append("\n## The prompts they edit, as they are")
        for name in touched:
            if name in pack.texts:
                parts.append(f"\n### {name}\n{wrap_untrusted(pack.text(name))}")
    parts.append(f"\nYou are composer {index + 1} of {of}.")
    return "\n".join(parts)


# ------------------------------------------------------------------------------- model calls


@dataclass
class TunerReply:
    text: str
    cost_usd: float = 0.0
    error: str | None = None


class Tuner(Protocol):
    """Who proposes and composes edits: a model (``ModelTuner``) or a script in tests."""

    model: str

    def estimate(self, system: str, prompt: str) -> float: ...

    async def ask(
        self, *, role: str, system: str, prompt: str, meta: dict[str, Any]
    ) -> TunerReply: ...


class ModelTuner:
    """One catalog model answers as proposer and composer, through the call gateway (prompts
    redacted, cost priced from the catalog)."""

    def __init__(
        self,
        model: str,
        *,
        models: dict[str, ModelEntry],
        providers: dict[str, ModelProvider],
        pricing: PricingRegistry,
        seed: int = 0,
    ) -> None:
        self.model = model
        self.entry = models[model]
        self.models = models
        self.providers = providers
        self.pricing = pricing
        self.seed = seed
        self.max_tokens = min(self.entry.max_tokens, TUNER_OUTPUT_TOKENS)

    def estimate(self, system: str, prompt: str) -> float:
        """Worst case for one call: the prompt at ~3 characters a token, full output budget."""
        cost = self.pricing.estimate_tokens_cost(
            provider=self.entry.provider,
            model_id=self.entry.model_id,
            input_tokens=(len(system) + len(prompt)) // 3 + 1,
            output_tokens=self.max_tokens,
            pricing_alias=self.model,
        )
        return cost.amount_usd or 0.0

    async def ask(self, *, role: str, system: str, prompt: str, meta: dict[str, Any]) -> TunerReply:
        if role not in (TUNE_PROPOSER_ROLE, TUNE_COMPOSER_ROLE):
            raise ValueError(f"unknown tuner role {role!r}")
        ledger = RunLedger()
        gateway = CallGateway(
            ledger=ledger,
            models=self.models,
            providers=self.providers,
            pricing=self.pricing,
            temperature=0.7,
            seed=self.seed + int(meta.get("index", 0)),
        )
        request = ModelRequest(
            model_id=self.entry.model_id,
            system_prompt=system,
            user_prompt=prompt,
            max_tokens=self.max_tokens,
            json_mode=True,
            metadata={"role": role, "personality": "judge", "tune": meta},
        )
        response = await gateway.call(stage="tune", alias=self.model, request=request)
        return TunerReply(
            text=response.content if response.ok else "",
            cost_usd=ledger.total_cost().usd,
            error=response.error,
        )
