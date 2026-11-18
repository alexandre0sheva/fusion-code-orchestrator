"""`fusion tune`: Propose–Probe–Compose over the prompt pack (after Harness²).

**Wide** (the default, Harness²'s parallel mode): K proposers per axis each suggest one edit to the
base pack; each valid edit is probed alone on the tuning slice of the dev split and compared with
the base; K composers build final packs from what was measured; the best final is kept only if it
beats the base by more than the noise band, and is then confirmed once on a held-out slice of the
dev split. **Deep** (sequential): K rounds, each proposing one edit to the best pack so far and
keeping it only if it beats that pack; the result is confirmed on the held-out slice the same way.

The test split is never loaded for probing. A run stops cleanly, with what it measured so far,
when its ``max_usd`` or the live-spend ledger would be passed. The output is a diff of the pack, an
evidence table (edit, observed effect, verdict) and, when a final is adopted, the pack itself as a
directory that ``FUSION_PROMPT_PACK`` can name.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from fusion.bench.runner import BenchEnv
from fusion.bench.spec import BenchTask, load_dataset, select_tasks
from fusion.bench.spend import SpendCapError
from fusion.config.prompt_pack import PROMPT_SPECS, Axis, PromptPack, default_pack
from fusion.library.store import Verdict
from fusion.providers.simulated_tune import TUNE_COMPOSER_ROLE, TUNE_PROPOSER_ROLE
from fusion.tune.compose import contrast, merge_edits
from fusion.tune.probe import (
    MIN_CATEGORY_TASKS,
    Effect,
    Probe,
    compare,
    probe,
    probe_config,
    split_holdout,
)
from fusion.tune.propose import (
    COMPOSER_SYSTEM,
    PROPOSER_SYSTEM,
    Edit,
    Tuner,
    compose_prompt,
    leaks,
    parse_composition,
    parse_proposal,
    propose_prompt,
    tune_material,
    validate_edits,
)
from fusion.verify.synthesis import assert_no_grader_material

__all__ = ["EvidenceRow", "TuneConfig", "TuneResult", "render_evidence", "run_tune"]

SPEND_TASK = "tune"
_WITHHELD = "(withheld: it contains grader material)"
_AXES: tuple[Axis, ...] = ("inv", "dom")
Stage = Literal["propose", "compose", "deep", "confirm"]
RowVerdict = Verdict | Literal["invalid", "duplicate", "not_probed", "confirmed", "not_confirmed"]


class TuneConfig(BaseModel):
    """Everything that defines a tuning run."""

    dataset: Path = Path("v1")
    arm: str = "auto"  # the strategy whose answers are scored
    mode: Literal["wide", "deep"] = "wide"
    k: int = Field(default=3, ge=1, le=8)  # proposers per axis and composers (wide); rounds (deep)
    axes: list[Axis] = Field(default_factory=lambda: list(_AXES), min_length=1)
    repeats: int = Field(default=1, ge=1)
    seed: int = 0
    limit: int | None = Field(default=24, ge=2)  # dev tasks used in all (tuning + held out)
    holdout_share: float = Field(default=0.25, ge=0.0, lt=1.0)
    min_gain: float = Field(default=0.02, ge=0.0)  # the smallest effect that counts
    min_category_tasks: int = Field(default=MIN_CATEGORY_TASKS, ge=1)  # to call `confounded`
    max_usd: float | None = Field(default=None, ge=0.0)
    mock: bool = False
    cache: bool = True
    concurrency: int = Field(default=8, ge=1)
    tuner_model: str | None = None
    max_prompt_chars: int = Field(default=4000, ge=200)
    max_growth_chars: int = Field(default=1500, ge=0)
    spend_stop_usd: float | None = Field(default=14.5, gt=0)  # roadmap rule: keep a reserve

    @model_validator(mode="after")
    def _cap_required_live(self) -> TuneConfig:
        if self.max_usd is None:
            if not self.mock:
                raise ValueError("max_usd is required for a live tuning run (pass --max-usd)")
            self.max_usd = 1000.0  # simulated: costs nothing real
        return self


class EvidenceRow(BaseModel):
    """One line of the evidence table: an edit (or a final, or the confirmation) and its effect."""

    id: str
    stage: Stage
    axis: str | None = None
    targets: list[str] = Field(default_factory=list)
    edit: str  # what changed, shortened
    rationale: str = ""
    edits: list[Edit] = Field(default_factory=list)
    pack_digest: str | None = None
    effect: Effect | None = None
    verdict: RowVerdict
    errors: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0  # tuner call + probe


class TuneResult(BaseModel):
    """The outcome of ``run_tune``; also written as ``tune.json``."""

    model_config = {"arbitrary_types_allowed": True}

    tune_id: str
    config: TuneConfig
    status: Literal["completed", "stopped"]
    stop_reason: str | None = None
    tuner_model: str
    base_digest: str
    base_score: float | None = None
    adopted_digest: str | None = None
    adopted: PromptPack | None = Field(default=None, exclude=True)
    best_digest: str | None = None
    tune_task_ids: list[str]
    holdout_task_ids: list[str]
    evidence: list[EvidenceRow]
    probes: int = 0
    spent_usd: float = 0.0
    out_dir: str = ""
    diff: str = Field(default="", exclude=True)


class _StopTuningError(Exception):
    """The budget ran out; the run ends with what it measured."""


class _Session:
    """State shared by the steps of one run: budget, probes by pack, the evidence so far."""

    def __init__(
        self,
        cfg: TuneConfig,
        env: BenchEnv,
        tuner: Tuner,
        tune_id: str,
        material: dict[str, list[str]],
    ) -> None:
        self.cfg = cfg
        self.env = env
        self.tuner = tuner
        self.tune_id = tune_id
        self.material = material
        self.max_usd = float(cfg.max_usd or 0.0)
        self.spent = 0.0
        self.probes: dict[tuple[str, str], Probe] = {}  # (slice, pack digest) -> probe
        self.runs = 0
        self.evidence: list[EvidenceRow] = []
        self.stop_reason: str | None = None
        self.spend = env.spend if not cfg.mock else None
        if self.spend is not None and cfg.spend_stop_usd is not None:
            from fusion.bench.spend import SpendLedger

            self.spend = SpendLedger(
                self.spend.path,
                budget=self.spend.budget,
                cap_usd=min(self.spend.cap_usd, cfg.spend_stop_usd),
            )

    def left(self) -> float:
        return self.max_usd - self.spent

    def stop(self, reason: str) -> _StopTuningError:
        self.stop_reason = reason
        return _StopTuningError(reason)

    # -- probing ---------------------------------------------------------------------------

    async def probe(self, pack: PromptPack, tasks: list[BenchTask], slice_: str) -> Probe:
        key = (slice_, pack.digest)
        if key in self.probes:
            return self.probes[key]
        if self.left() <= 1e-9:
            raise self.stop(f"max_usd reached: ${self.spent:.4f} of ${self.max_usd:.2f} spent")
        self.runs += 1
        cfg = probe_config(
            dataset=self.cfg.dataset,
            arm=self.cfg.arm,
            repeats=self.cfg.repeats,
            seed=self.cfg.seed,
            max_usd=self.left(),
            mock=self.cfg.mock,
            cache=self.cfg.cache,
            concurrency=self.cfg.concurrency,
            spend_stop_usd=self.cfg.spend_stop_usd,
        )
        label = f"{slice_}-{pack.digest[:8]}"
        try:
            result = await probe(
                pack,
                tasks,
                env=self.env,
                cfg=cfg,
                label=label,
                run_id=f"{self.tune_id}-{self.runs:02d}-{label}",
            )
        except SpendCapError as exc:
            raise self.stop(str(exc)) from exc
        self.spent += result.cost_usd
        if result.stopped:
            raise self.stop(result.stop_reason or f"probe {label} ended {result.status}")
        self.probes[key] = result
        return result

    # -- tuner calls ------------------------------------------------------------------------

    async def ask(
        self, role: str, system: str, prompt: str, meta: dict[str, Any]
    ) -> tuple[str, float]:
        assert_no_grader_material(prompt, self.material)  # a leak here is a bug, not a result
        estimate = self.tuner.estimate(system, prompt)
        if estimate > self.left() + 1e-9:
            raise self.stop(
                f"max_usd reached: ${self.spent:.4f} of ${self.max_usd:.2f} spent and the next "
                f"tuner call may cost up to ${estimate:.4f}"
            )
        if self.spend is not None:
            try:
                self.spend.check(estimate, f"tune {role}")
            except SpendCapError as exc:
                raise self.stop(str(exc)) from exc
        reply = await self.tuner.ask(role=role, system=system, prompt=prompt, meta=meta)
        self.spent += reply.cost_usd
        if self.spend is not None and reply.cost_usd > 0:
            self.spend.append(reply.cost_usd, task=SPEND_TASK, purpose=f"{self.tune_id} {role}")
        return reply.text, reply.cost_usd

    def validate(
        self, current: PromptPack, edits: Sequence[Edit], base: PromptPack
    ) -> tuple[PromptPack | None, list[str]]:
        return validate_edits(
            current,
            edits,
            material=self.material,
            reference=base,
            max_prompt_chars=self.cfg.max_prompt_chars,
            max_growth_chars=self.cfg.max_growth_chars,
        )


def _targets(axis: Axis) -> list[str]:
    return [name for name, spec in PROMPT_SPECS.items() if spec.axis == axis]


def _describe(edits: Sequence[Edit]) -> str:
    return " + ".join(e.summary(80 if len(edits) > 1 else 120) for e in edits) or "(nothing)"


async def _propose_one(
    s: _Session,
    *,
    current: PromptPack,
    base: PromptPack,
    reference: Probe,
    tasks: list[BenchTask],
    axis: Axis,
    index: int,
    of: int,
    earlier: list[str],
    row_id: str,
    stage: Stage,
) -> tuple[EvidenceRow, PromptPack | None, Probe | None]:
    """Ask for one edit, validate it, probe it, compare it with ``reference``."""
    prompt = propose_prompt(
        pack=current,
        axis=axis,
        scores=reference.by_category(),
        earlier=earlier,
        index=index,
        of=of,
    )
    meta = {"stage": "propose", "axis": axis, "index": index, "targets": _targets(axis)}
    text, cost = await s.ask(TUNE_PROPOSER_ROLE, PROPOSER_SYSTEM, prompt, meta)
    edit, error = parse_proposal(text)
    if edit is None:
        row = EvidenceRow(
            id=row_id,
            stage=stage,
            axis=axis,
            edit="(unreadable)",
            verdict="invalid",
            errors=[error or "no edit"],
            cost_usd=cost,
        )
        return row, None, None
    if edit.target in PROMPT_SPECS and PROMPT_SPECS[edit.target].axis != axis:
        errors = [f"'{edit.target}' is not a {axis} prompt"]
        pack = None
    else:
        pack, errors = s.validate(current, [edit], base)
    if not leaks(errors):  # a leaking edit is never echoed to the next proposer
        earlier.append(edit.summary())
    row = EvidenceRow(
        id=row_id,
        stage=stage,
        axis=axis,
        targets=[edit.target],
        edit=_WITHHELD if leaks(errors) else edit.summary(),
        rationale="" if leaks(errors) else edit.rationale,
        edits=[] if leaks(errors) else [edit],
        verdict="invalid",
        errors=errors,
        cost_usd=cost,
    )
    if pack is None:
        return row, None, None
    try:
        result = await s.probe(pack, tasks, "tune")
    except _StopTuningError as stop:  # the table still shows the edit that was not measured
        s.evidence.append(row.model_copy(update={"verdict": "not_probed", "errors": [str(stop)]}))
        raise
    effect = compare(
        reference,
        result,
        min_gain=s.cfg.min_gain,
        seed=s.cfg.seed,
        min_category_tasks=s.cfg.min_category_tasks,
    )
    row = row.model_copy(
        update={
            "pack_digest": pack.digest,
            "effect": effect,
            "verdict": effect.verdict,
            "cost_usd": cost + result.cost_usd,
        }
    )
    return row, pack, result


async def _wide(
    s: _Session, base: PromptPack, base_probe: Probe, tasks: list[BenchTask]
) -> PromptPack | None:
    """Propose along each axis, probe each edit alone, compose finals; the best final that beats
    the base by more than the noise band (or None)."""
    measured: list[tuple[str, Edit, Effect]] = []
    for axis in s.cfg.axes:
        earlier: list[str] = []
        for index in range(s.cfg.k):
            row_id = f"p{len(s.evidence) + 1}"
            row, _, _ = await _propose_one(
                s,
                current=base,
                base=base,
                reference=base_probe,
                tasks=tasks,
                axis=axis,
                index=index,
                of=s.cfg.k,
                earlier=earlier,
                row_id=row_id,
                stage="propose",
            )
            s.evidence.append(row)
            if row.effect is not None:
                measured.append((row.id, row.edits[0], row.effect))
    helped = [(e, eff.delta) for _, e, eff in measured if eff.verdict == "helped"]
    if not helped:
        return None
    finals: list[tuple[EvidenceRow, PromptPack]] = []
    contrasts = [contrast(rid, edit, eff) for rid, edit, eff in measured]
    seen: set[str] = set()
    for index in range(s.cfg.k):
        row_id = f"f{index + 1}"
        cost = 0.0
        rationale = ""
        if index == 0:
            edits = merge_edits(helped)
            rationale = "every edit that helped alone, strongest first"
            error = None
        else:
            prompt = compose_prompt(pack=base, contrasts=contrasts, index=index, of=s.cfg.k)
            meta = {
                "stage": "compose",
                "index": index,
                "edits": [
                    {**e.model_dump(include={"target", "op", "text"}), "gain": gain}
                    for e, gain in helped
                ],
            }
            text, cost = await s.ask(TUNE_COMPOSER_ROLE, COMPOSER_SYSTEM, prompt, meta)
            edits, error = parse_composition(text)
        pack, errors = s.validate(base, edits, base) if error is None else (None, [error])
        row = EvidenceRow(
            id=row_id,
            stage="compose",
            targets=sorted({e.target for e in edits}),
            edit=_WITHHELD if leaks(errors) else _describe(edits),
            rationale=rationale,
            edits=[] if leaks(errors) else edits,
            verdict="invalid",
            errors=errors,
            cost_usd=cost,
        )
        if pack is None:
            s.evidence.append(row)
            continue
        if pack.digest in seen:
            s.evidence.append(
                row.model_copy(
                    update={"verdict": "duplicate", "errors": ["the same pack as an earlier final"]}
                )
            )
            continue
        seen.add(pack.digest)
        result = await s.probe(pack, tasks, "tune")
        effect = compare(
            base_probe,
            result,
            min_gain=s.cfg.min_gain,
            seed=s.cfg.seed,
            min_category_tasks=s.cfg.min_category_tasks,
        )
        row = row.model_copy(
            update={
                "pack_digest": pack.digest,
                "effect": effect,
                "verdict": effect.verdict,
                "cost_usd": cost + result.cost_usd,
            }
        )
        s.evidence.append(row)
        finals.append((row, pack))
    winners = [(r, p) for r, p in finals if r.verdict == "helped" and r.effect is not None]
    if not winners:
        return None
    return max(winners, key=lambda pair: pair[0].effect.delta if pair[0].effect else 0.0)[1]


async def _deep(
    s: _Session, base: PromptPack, base_probe: Probe, tasks: list[BenchTask]
) -> PromptPack | None:
    """K rounds, each proposing one edit to the best pack so far and keeping it if it beats it."""
    current, current_probe = base, base_probe
    earlier: dict[str, list[str]] = {axis: [] for axis in s.cfg.axes}
    per_axis: dict[str, int] = dict.fromkeys(s.cfg.axes, 0)
    for round_ in range(s.cfg.k):
        axis = s.cfg.axes[round_ % len(s.cfg.axes)]
        row, pack, result = await _propose_one(
            s,
            current=current,
            base=base,
            reference=current_probe,
            tasks=tasks,
            axis=axis,
            index=per_axis[axis],
            of=s.cfg.k,
            earlier=earlier[axis],
            row_id=f"d{round_ + 1}",
            stage="deep",
        )
        per_axis[axis] += 1
        s.evidence.append(row)
        if row.verdict == "helped" and pack is not None and result is not None:
            current, current_probe = pack, result
    return None if current.digest == base.digest else current


async def run_tune(
    cfg: TuneConfig,
    *,
    env: BenchEnv,
    tuner: Tuner,
    base: PromptPack | None = None,
    out_dir: Path | None = None,
    tune_id: str | None = None,
) -> TuneResult:
    """Tune ``base`` (the pack in use by default) and write the outputs under ``out_dir``."""
    base = base or default_pack()
    tune_id = tune_id or f"tune-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    dev = select_tasks(load_dataset(cfg.dataset, "dev"), cfg.limit, cfg.seed)
    tasks, holdout = split_holdout(dev, cfg.holdout_share, cfg.seed)
    material = tune_material(load_dataset(cfg.dataset, "all"))
    s = _Session(cfg, env, tuner, tune_id, material)
    adopted: PromptPack | None = None
    best: PromptPack | None = None
    base_probe: Probe | None = None
    try:
        if s.spend is not None:
            try:
                s.spend.check(0.0, "starting a tuning run")
            except SpendCapError as exc:
                raise s.stop(str(exc)) from exc
        base_probe = await s.probe(base, tasks, "tune")
        best = await (_wide if cfg.mode == "wide" else _deep)(s, base, base_probe, tasks)
        if best is not None:
            adopted = await _confirm(s, base, best, holdout)
    except _StopTuningError:
        pass
    result = TuneResult(
        tune_id=tune_id,
        config=cfg,
        status="stopped" if s.stop_reason else "completed",
        stop_reason=s.stop_reason,
        tuner_model=tuner.model,
        base_digest=base.digest,
        base_score=base_probe.mean() if base_probe else None,
        adopted_digest=adopted.digest if adopted else None,
        adopted=adopted,
        best_digest=best.digest if best else None,
        tune_task_ids=[t.id for t in tasks],
        holdout_task_ids=[t.id for t in holdout],
        evidence=s.evidence,
        probes=s.runs,
        spent_usd=s.spent,
        diff=base.diff(adopted) if adopted else "",
    )
    if out_dir is not None:
        _write(result, base, out_dir)
    return result


async def _confirm(
    s: _Session, base: PromptPack, best: PromptPack, holdout: list[BenchTask]
) -> PromptPack | None:
    """Probe the base and the best final once on the held-out slice; adopt the final when it
    holds up there: a mean effect above zero that is neither ``hurt`` nor ``confounded``. The
    slice is small, so clearing the noise band again is not required."""
    changed = base.changed(best)
    if not holdout:
        s.evidence.append(
            EvidenceRow(
                id="h1",
                stage="confirm",
                targets=changed,
                edit="held-out confirmation",
                pack_digest=best.digest,
                verdict="not_confirmed",
                errors=["no held-out tasks (the dev split is too small); nothing adopted"],
            )
        )
        return None
    ref = await s.probe(base, holdout, "holdout")
    result = await s.probe(best, holdout, "holdout")
    effect = compare(
        ref,
        result,
        min_gain=s.cfg.min_gain,
        seed=s.cfg.seed,
        min_category_tasks=s.cfg.min_category_tasks,
    )
    confirmed = effect.delta > 0 and effect.verdict not in ("hurt", "confounded")
    s.evidence.append(
        EvidenceRow(
            id="h1",
            stage="confirm",
            targets=changed,
            edit=f"held-out confirmation of the best final ({len(holdout)} tasks)",
            pack_digest=best.digest,
            effect=effect,
            verdict="confirmed" if confirmed else "not_confirmed",
            cost_usd=ref.cost_usd + result.cost_usd,
        )
    )
    return best if confirmed else None


# ----------------------------------------------------------------------------------- output


def render_evidence(result: TuneResult) -> str:
    """The evidence table as Markdown: edit → observed effect → verdict."""
    cfg = result.config
    lines = [
        f"# Tuning {result.tune_id}",
        "",
        f"Dataset `{cfg.dataset}` (dev split: {len(result.tune_task_ids)} tuning tasks, "
        f"{len(result.holdout_task_ids)} held out), arm `{cfg.arm}`, mode {cfg.mode}, k={cfg.k}, "
        f"repeats {cfg.repeats}, tuner `{result.tuner_model}`"
        + (" (simulated)" if cfg.mock else "")
        + ".",
        f"Base pack `{result.base_digest}` scored "
        + (f"{result.base_score:.3f}" if result.base_score is not None else "n/a")
        + f" mean quality. {result.probes} probe(s), ${result.spent_usd:.4f} spent.",
        "",
        "| id | stage | axis | prompt(s) | edit | Δ quality [95% CI] | band | by task type "
        "| verdict |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in result.evidence:
        eff = row.effect
        cats = ", ".join(f"{c} {d:+.2f}" for c, d in eff.by_category.items()) if eff else ""
        note = row.verdict + (f": {'; '.join(row.errors)}" if row.errors else "")
        if eff and row.verdict not in ("invalid",):
            note += f" ({eff.reason})"
        cells = [
            row.id,
            row.stage,
            row.axis or "",
            ", ".join(row.targets),
            row.edit,
            eff.text() if eff else "",
            f"{eff.band:.3f}" if eff else "",
            cats,
            note,
        ]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    lines.append("")
    if result.adopted_digest:
        lines.append(
            f"**Adopted** pack `{result.adopted_digest}`: it beat the base by more than the noise "
            "band on the tuning tasks and held up on the held-out tasks. See `pack.diff`."
        )
    elif result.best_digest:
        lines.append(
            f"Best final `{result.best_digest}` did not hold up on the held-out tasks: the base "
            "pack is kept."
        )
    else:
        lines.append("No final beat the base by more than the noise band: the base pack is kept.")
    if result.stop_reason:
        lines.append(f"\nStopped early: {result.stop_reason}")
    if cfg.mock:
        lines.append(
            "\nSimulated run: the effects are planted in the simulator (see `simulated_tune`), so "
            "this shows the procedure, not that any edit helps a real model."
        )
    return "\n".join(lines) + "\n"


def _write(result: TuneResult, base: PromptPack, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result.out_dir = str(out_dir)
    (out_dir / "pack.diff").write_text(result.diff, encoding="utf-8")
    (out_dir / "evidence.md").write_text(render_evidence(result), encoding="utf-8")
    if result.adopted is not None:
        result.adopted.write(out_dir / "pack")
    payload = result.model_dump(mode="json")
    (out_dir / "tune.json").write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
