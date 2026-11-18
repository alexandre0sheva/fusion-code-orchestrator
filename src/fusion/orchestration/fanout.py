"""Concurrent fan-out to panel models with timeout and quorum controls."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from fusion.config.catalog import same_tier_spares, spare_tier
from fusion.config.loader import EarlyReturn, FanoutConfig, ModelEntry
from fusion.orchestration.claims import panel_answer_schema
from fusion.orchestration.ledger import CallGateway, call_status, standalone_gateway
from fusion.orchestration.progress import report as report_progress
from fusion.orchestration.prompts import (
    approach_instruction,
    compose_user_prompt,
    get_system_prompt,
    lens_instruction,
    role_instruction,
    self_check_instruction,
)
from fusion.orchestration.strategy import PanelMember, member_overrides
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.routing.budget import ANSWER_OUTPUT_TOKENS
from fusion.routing.classifier import TaskType
from fusion.verify.synthesis import checks_instruction

if TYPE_CHECKING:
    from fusion.orchestration.approaches import Approach
    from fusion.orchestration.lenses import Lens

PanelStatus = Literal["success", "failed", "timeout", "missing_provider", "cancelled"]
# Failures after which a member is asked of a same-tier model instead: the model is unknown or
# retired (404/410), or its provider is down, overloaded or unreachable after the retries.
FALLBACK_ERRORS = frozenset({"NotFound", "Server", "RateLimit", "Connection"})


class PanelCallResult(BaseModel):
    """Structured outcome for a single panel model call."""

    model_name: str
    provider: str
    provider_model_id: str
    status: PanelStatus
    response: ModelResponse | None = None
    error: str | None = None
    error_type: str | None = None
    latency_ms: int = 0

    @property
    def success(self) -> bool:
        return self.status == "success" and self.response is not None and self.response.ok


class FanoutResult(BaseModel):
    """Aggregate result of panel fan-out."""

    calls: list[PanelCallResult] = Field(default_factory=list)
    panel_wall_latency_ms: int = 0
    total_model_call_latency_ms: int = 0
    max_model_latency_ms: int = 0
    min_successful_responses: int = 1
    quorum_met: bool = True
    timed_out: bool = False
    early_return: bool = False  # stopped waiting for stragglers once quorum was in
    grace_ms: int | None = None  # how long stragglers were given after the quorum
    hedged: dict[str, str] = Field(default_factory=dict)  # slow member -> model also asked
    # failed member -> same-tier model asked instead ("" when there was none to ask)
    fallbacks: dict[str, str] = Field(default_factory=dict)
    # answering model -> the member it answered for (differs after a hedge or a fallback)
    answered_for: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def successful(self) -> list[tuple[str, ModelResponse]]:
        return [
            (call.model_name, call.response)
            for call in self.calls
            if call.success and call.response is not None
        ]

    @property
    def failed_count(self) -> int:
        return len([call for call in self.calls if not call.success])

    @property
    def success_count(self) -> int:
        return len(self.successful)


async def fanout_to_panel(
    *,
    panel_models: list[str],
    registry_models: dict[str, ModelEntry],
    providers: dict[str, ModelProvider],
    task_type: TaskType,
    primary_content: str,
    context: str = "",
    file_snippets: list[str] | None = None,
    changed_files: list[str] | None = None,
    lessons: str = "",
    config: FanoutConfig | None = None,
    gateway: CallGateway | None = None,
    members: Mapping[str, PanelMember] | None = None,
    min_successful: int | None = None,
    label: str = "panel",
    avoid: Collection[str] = (),
    lenses: Mapping[str, Lens] | None = None,
    approaches: Mapping[str, Approach] | None = None,
    self_check: bool = False,
    checks: bool = False,
) -> FanoutResult:
    """Call all panel models concurrently and return structured outcomes.

    Every call is recorded by ``gateway`` (a throwaway one when the caller has no run ledger).
    ``members`` carries per-model settings (role, temperature, reasoning effort) from a strategy.
    All calls start at once; ``max_concurrency`` caps in-flight calls per provider, so a slow
    provider's queue never holds up another's. With ``early_return`` the fan-out stops waiting
    shortly after quorum; with ``hedge_after_ms`` a slow member is re-asked of another model.
    A member whose call fails for a reason in ``FALLBACK_ERRORS`` is asked of a same-tier model
    instead, so one retired or failing model does not cost the quorum. Hedges and fallbacks never
    ask a model in ``avoid`` (the strategy's other members). ``min_successful`` replaces the
    configured quorum (a cascade's first wave needs two answers whatever the panel's quorum is).
    ``label`` names the wave in progress messages ("panel 2/3 done"). ``lenses`` gives members
    their lens (``lenses.assign_lenses``) and ``approaches`` their stance
    (``approaches.assign_approaches``); a hedge or fallback asked for a member keeps both.
    ``self_check`` asks every member to check its answer and report an honest confidence;
    ``checks`` asks every member for check scripts and guardrails with its patch (verified-synth).
    ``lessons`` is the experience library's block for this request (``fusion.library``).
    Cancelling the caller cancels every call still in flight.
    """
    fanout_config = config or FanoutConfig()
    panel = _Panel(
        panel_models=panel_models,
        registry_models=registry_models,
        providers=providers,
        config=fanout_config,
        gateway=gateway or standalone_gateway(registry_models, providers),
        members=members or {},
        task_type=task_type,
        primary_content=primary_content,
        context=context,
        file_snippets=file_snippets,
        changed_files=changed_files,
        lessons=lessons,
        min_successful=min_successful,
        label=label,
        avoid=avoid,
        lenses=lenses or {},
        approaches=approaches or {},
        self_check=self_check,
        checks=checks,
    )
    return await panel.run()


class _Attempt:
    """One call of one panel member (the original, or a hedge to another model)."""

    def __init__(self, member: str, model: str, task: asyncio.Task[PanelCallResult]) -> None:
        self.member, self.model, self.task = member, model, task
        self.started = asyncio.get_running_loop().time()
        self.result: PanelCallResult | None = None


class _Panel:
    """One fan-out: launches the attempts and decides when to stop waiting for them."""

    def __init__(
        self,
        *,
        panel_models: list[str],
        registry_models: dict[str, ModelEntry],
        providers: dict[str, ModelProvider],
        config: FanoutConfig,
        gateway: CallGateway,
        members: Mapping[str, PanelMember],
        task_type: TaskType,
        primary_content: str,
        context: str = "",
        file_snippets: list[str] | None = None,
        changed_files: list[str] | None = None,
        lessons: str = "",
        min_successful: int | None = None,
        label: str = "panel",
        avoid: Collection[str] = (),
        lenses: Mapping[str, Lens] | None = None,
        approaches: Mapping[str, Approach] | None = None,
        self_check: bool = False,
        checks: bool = False,
    ) -> None:
        self.label = label
        self.self_check = self_check
        self.checks = checks
        self.lenses = dict(lenses or {})
        self.approaches = dict(approaches or {})
        self.panel_models = panel_models
        self.registry_models = registry_models
        self.providers = providers
        self.config = config
        self.gateway = gateway
        self.members = members
        self.avoid = frozenset(avoid)
        # Every member gets the same system prompt and the same prompt prefix (a member's role is
        # appended at the end), so providers' prompt caches can reuse it across calls.
        prompt = compose_user_prompt(
            task_type=task_type,
            primary_content=primary_content,
            context=context,
            file_snippets=file_snippets,
            changed_files=changed_files,
            lessons=lessons,
        )
        self.user_prompt = prompt.text
        self.shared_prefix = prompt.shared_prefix
        self.task_type = task_type
        self.system_prompt = get_system_prompt(task_type)
        self.schema = panel_answer_schema(checks=checks)
        needed = min_successful or config.min_successful_responses
        self.min_success = min(needed, max(len(panel_models), 1))
        self._slots: dict[str, asyncio.Semaphore] = {}
        self._attempts: list[_Attempt] = []
        self._hedged: dict[str, str] = {}  # slow member -> model that was asked instead
        self._fallbacks: dict[str, str] = {}  # failed member -> model that was asked instead
        self._grace_ms: float | None = None
        self._cancel_message = "Cancelled by global panel timeout"
        self._reported = 0

    # -- one call ---------------------------------------------------------------------------

    def _slot(self, provider: str) -> asyncio.Semaphore:
        if provider not in self._slots:
            self._slots[provider] = asyncio.Semaphore(self.config.max_concurrency)
        return self._slots[provider]

    def _cache_prefix(self) -> bool:
        """Mark a prompt-cache breakpoint (explicit-cache providers) on this fan-out's calls?"""
        mode = self.config.prompt_cache
        return mode == "always" or (mode == "auto" and self.shared_prefix > 0)

    def _lens_text(self, lens: Lens) -> str:
        others = {o.name: o for o in self.lenses.values() if o.name != lens.name}
        return lens_instruction(lens, list(others.values()))

    async def _call(
        self,
        alias: str,
        member: PanelMember | None,
        lens: Lens | None = None,
        approach: Approach | None = None,
    ) -> PanelCallResult:
        entry = self.registry_models[alias]
        explicit = member.role if member and member.role != "auto" else None
        # A lens or an approach replaces the catalog persona (a security persona would fight a
        # correctness lens, a stance its own persona); a role the strategy sets on purpose is kept.
        stanced = lens is not None or approach is not None
        persona = explicit if stanced else explicit or entry.persona
        timeout = self.config.per_model_timeout_seconds
        suffix = (role_instruction(persona) if persona else "") + (
            self._lens_text(lens) if lens is not None else ""
        ) + (approach_instruction(approach) if approach is not None else "") + (
            self_check_instruction() if self.self_check else ""
        ) + (checks_instruction() if self.checks else "")
        metadata = {"task_type": self.task_type.value, "role": "panel", "personality": persona}
        if lens is not None:
            metadata["lens"] = lens.name
        if approach is not None:
            metadata["approach"] = approach.name
        request = ModelRequest(
            model_id=entry.model_id,
            system_prompt=self.system_prompt,
            user_prompt=self.user_prompt + suffix,
            max_tokens=entry.max_tokens,
            response_schema=self.schema,
            response_schema_name="panel_answer",
            cache_prefix=self._cache_prefix(),
            shared_prefix_chars=self.shared_prefix or None,
            timeout=timeout,
            metadata=metadata,
            **member_overrides(member),
        )
        async with self._slot(entry.provider):  # the timeout starts once a slot is free
            response = await self.gateway.call(
                stage="panel", alias=alias, request=request, timeout=timeout
            )
        return _panel_result(alias, entry, response, self.config, self._cancel_message)

    def _launch(self, member: str, model: str) -> None:
        # A hedge or fallback asks another model with its own catalog settings, not the member's,
        # but for the member's lens: the area still needs covering.
        settings = self.members.get(member) if model == member else None
        task = asyncio.create_task(
            self._call(model, settings, self.lenses.get(member), self.approaches.get(member))
        )
        self._attempts.append(_Attempt(member, model, task))

    # -- the wait ---------------------------------------------------------------------------

    def _settle(self) -> None:
        for attempt in list(self._attempts):
            if attempt.result is None and attempt.task.done() and not attempt.task.cancelled():
                attempt.result = attempt.task.result()
                if attempt.result.success:
                    self._cancel_rivals(attempt)
                else:
                    self._fall_back(attempt)

    def _fall_back(self, failed: _Attempt) -> None:
        """A member's own call failed for good: ask a same-tier model instead, once."""
        result = failed.result
        assert result is not None
        if (
            failed.model != failed.member
            or result.error_type not in FALLBACK_ERRORS
            or failed.member in self._fallbacks
            or any(a.member == failed.member and not a.task.done() for a in self._attempts)
        ):
            return  # a hedge is still running for the member, or there was a fallback already
        spare = self._spare(failed.member)
        self._fallbacks[failed.member] = spare or ""
        if spare:
            self._launch(failed.member, spare)

    def _cancel_rivals(self, winner: _Attempt) -> None:
        """A member answered: its other attempt (a hedge or the slow original) is not needed."""
        for other in self._attempts:
            if other.member == winner.member and other is not winner and not other.task.done():
                other.task.cancel()

    def _member_answered(self, member: str) -> bool:
        return any(a.member == member and a.result and a.result.success for a in self._attempts)

    def _successes(self) -> int:
        return sum(self._member_answered(m) for m in self.panel_models)

    def _live(self) -> set[asyncio.Task[PanelCallResult]]:
        return {a.task for a in self._attempts if not a.task.done()}

    def _usable(self, alias: str) -> bool:
        provider = self.providers.get(self.registry_models[alias].provider)
        return provider is not None and provider.is_available()

    def _spare(self, member: str) -> str | None:
        """The model to ask in a member's place: same price tier, another vendor first, then
        the fastest by measured speed. Never one already asked or one of the other members."""
        taken = {a.model for a in self._attempts} | set(self.panel_models) | self.avoid
        spares = same_tier_spares(
            self.registry_models,
            member,
            output_tokens=ANSWER_OUTPUT_TOKENS,
            exclude=taken,
            usable=self._usable,
        )
        return spares[0] if spares else None

    def _hedge_due(self) -> float | None:
        """Loop time at which the next slow, unhedged member should be hedged."""
        after = self.config.hedge_after_ms
        if after is None:
            return None
        waiting = [
            a.started + after / 1000
            for a in self._attempts
            if a.model == a.member and a.member not in self._hedged and not a.task.done()
        ]
        return min(waiting) if waiting else None

    def _hedge_slow_members(self, now: float) -> None:
        due = self._hedge_due()
        while due is not None and due <= now:
            slow = next(
                a
                for a in self._attempts
                if a.model == a.member
                and a.member not in self._hedged
                and not a.task.done()
                and a.started + (self.config.hedge_after_ms or 0) / 1000 <= now
            )
            candidate = self._spare(slow.member)
            self._hedged[slow.member] = candidate or ""
            if candidate:
                self._launch(slow.member, candidate)
            due = self._hedge_due()

    def _finished(self) -> int:
        """Members that have an outcome: an answer, or a failure with nothing left running."""
        done = 0
        for member in self.panel_models:
            mine = [a for a in self._attempts if a.member == member]
            answered = any(a.result is not None and a.result.success for a in mine)
            gave_up = all(a.task.done() for a in mine) and any(a.result for a in mine)
            done += answered or gave_up
        return done

    def _answer_tokens(self) -> int:
        """Output length to expect from a straggler: what the members who answered wrote."""
        sizes = [
            a.result.response.output_tokens
            for a in self._attempts
            if a.result is not None
            and a.result.success
            and a.result.response is not None
            and a.result.response.output_tokens
        ]
        return round(sum(sizes) / len(sizes)) if sizes else ANSWER_OUTPUT_TOKENS

    def _grace_s(self, early: EarlyReturn, now: float) -> float:
        """Seconds stragglers get once the quorum is in (see ``EarlyReturn``)."""
        cap = early.max_grace_ms / 1000
        if early.grace_ms != "auto":
            return early.grace_ms / 1000
        tokens = self._answer_tokens()
        expected: list[float] = []
        for attempt in self._attempts:
            entry = self.registry_models.get(attempt.model)
            if attempt.task.done() or entry is None:
                continue
            ms = entry.expected_answer_ms(tokens)
            if ms is not None:
                expected.append(attempt.started + ms / 1000)
        if not expected:
            return cap  # nothing measured: give stragglers the most the policy allows
        # Wait for the next straggler only if it is due within the cap: waiting for one that is
        # overdue, or due later than the cap, buys nothing.
        due = [t - now for t in expected if now < t <= now + cap]
        return min(due) if due else 0.0

    async def _report_progress(self) -> None:
        done = self._finished()
        while self._reported < done:  # members that finish together are still reported one by one
            self._reported += 1
            await report_progress(f"{self.label} {self._reported}/{len(self.panel_models)} done")

    async def run(self) -> FanoutResult:
        try:
            return await self._run()
        except asyncio.CancelledError:
            # The caller gave up: no call may outlive it (a straggler is still billed).
            await self._cancel_all()
            raise

    async def _cancel_all(self) -> None:
        live = self._live()
        for task in live:
            task.cancel()
        if live:
            await asyncio.gather(*live, return_exceptions=True)

    async def _run(self) -> FanoutResult:
        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        early = self.config.early_return
        stop_at = max(early.quorum, self.min_success) if early else None
        for alias in self.panel_models:
            self._launch(alias, alias)
        deadline = loop.time() + self.config.global_timeout_seconds
        quorum_deadline: float | None = None
        timed_out = early_returned = False
        while self._live():
            wake = min(
                t for t in (deadline, quorum_deadline, self._hedge_due()) if t is not None
            )
            await asyncio.wait(
                self._live(),
                timeout=max(wake - loop.time(), 0),
                return_when=asyncio.FIRST_COMPLETED,
            )
            self._settle()
            await self._report_progress()
            now = loop.time()
            self._hedge_slow_members(now)
            reached = stop_at is not None and self._successes() >= stop_at
            if early and reached and quorum_deadline is None:
                grace = self._grace_s(early, now)
                self._grace_ms = grace * 1000
                quorum_deadline = now + grace
            if now >= deadline:
                timed_out = True
                break
            if quorum_deadline is not None and now >= quorum_deadline:
                early_returned = bool(self._live())
                break
        if self._live():
            await self._stop_stragglers(timed_out, early_returned)
        return self._summarize(started, timed_out, early_returned)

    async def _stop_stragglers(self, timed_out: bool, early_returned: bool) -> None:
        if timed_out and not self.config.cancel_on_global_timeout:
            return
        if early_returned:
            self._cancel_message = "Cancelled after quorum (early return)"
        live = self._live()
        for task in live:
            task.cancel()
        await asyncio.wait(live, timeout=1.0)
        self._settle()

    # -- the outcome ------------------------------------------------------------------------

    def _outcome(self, member: str) -> PanelCallResult:
        """The member's answer, else its original attempt's failure, else a placeholder."""
        mine = [a for a in self._attempts if a.member == member]
        for attempt in mine:
            if attempt.result and attempt.result.success:
                return attempt.result
        original = next(a for a in mine if a.model == member)
        if original.result is not None:
            return original.result
        return _pending_result(member, original.task, self.registry_models, self._cancel_message)

    def _summarize(self, started: float, timed_out: bool, early_returned: bool) -> FanoutResult:
        calls = [self._outcome(member) for member in self.panel_models]
        result = _summarize(calls, started, self.min_success, timed_out, self.config)
        for member, model in self._hedged.items():
            result.warnings.append(
                f"Panel model {member} was slow; asked {model} as well"
                if model
                else f"Panel model {member} was slow; no other panel model was available to ask"
            )
        for member, model in self._fallbacks.items():
            tier = spare_tier(self.registry_models[member])
            result.warnings.append(
                f"Panel model {member} failed; asked {model} (same tier: {tier}) instead"
                if model
                else f"Panel model {member} failed; no other {tier}-tier panel model was available"
            )
        if early_returned:
            result.early_return = True
            stopped = [c.model_name for c in calls if c.status == "cancelled"]
            result.warnings.append(
                f"Early return: {self._successes()} answers in; stopped waiting for "
                f"{', '.join(stopped) or 'stragglers'}"
            )
        result.hedged = dict(self._hedged)
        result.fallbacks = dict(self._fallbacks)
        result.answered_for = {
            call.model_name: member for member, call in zip(self.panel_models, calls, strict=True)
        }
        if self._grace_ms is not None:
            result.grace_ms = round(self._grace_ms)
        return result


def _panel_result(
    model_name: str,
    entry: ModelEntry,
    response: ModelResponse,
    config: FanoutConfig,
    cancel_message: str = "Cancelled by global panel timeout",
) -> PanelCallResult:
    status = call_status(response)
    common = {
        "model_name": model_name,
        "provider": entry.provider,
        "provider_model_id": entry.model_id,
        "latency_ms": round(response.latency_ms),
    }
    if status == "success":
        return PanelCallResult(status="success", response=response, **common)  # type: ignore[arg-type]
    if status == "failed":
        return PanelCallResult(
            status="failed",
            response=response,
            error=response.error,
            error_type=response.error_type or "ProviderError",
            **common,  # type: ignore[arg-type]
        )
    messages = {
        "missing_provider": response.error,
        "timeout": f"Timed out after {config.per_model_timeout_seconds:.1f}s",
        "cancelled": cancel_message,
    }
    common["latency_ms"] = 0 if status == "missing_provider" else common["latency_ms"]
    return PanelCallResult(
        status=status,
        error=messages[status],
        error_type=response.error_type,
        **common,  # type: ignore[arg-type]
    )


def _pending_result(
    model_name: str,
    task: asyncio.Task[PanelCallResult],
    registry_models: dict[str, ModelEntry],
    cancel_message: str,
) -> PanelCallResult:
    """Result of a call that never finished, attributed to its own model."""
    entry = registry_models.get(model_name)
    cancelled = task.cancelled()
    return PanelCallResult(
        model_name=model_name,
        provider=entry.provider if entry else "<unknown>",
        provider_model_id=entry.model_id if entry else "<unknown>",
        status="cancelled" if cancelled else "timeout",
        error=cancel_message if cancelled else "Still pending after global panel timeout",
        error_type="CancelledError" if cancelled else "TimeoutError",
    )


def _summarize(
    calls: list[PanelCallResult],
    started: float,
    min_success: int,
    timed_out: bool,
    config: FanoutConfig,
) -> FanoutResult:
    panel_wall = round((time.perf_counter() - started) * 1000)
    success_count = len([call for call in calls if call.success])
    quorum_met = success_count >= min_success

    warnings: list[str] = []
    if timed_out:
        warnings.append(
            f"Panel global timeout after {config.global_timeout_seconds:.1f}s; "
            "slow calls were cancelled."
        )
    for call in calls:
        if not call.success:
            warnings.append(f"Panel model {call.model_name} {call.status}: {call.error}")
    if not quorum_met:
        warnings.append(
            f"Panel quorum not met: {success_count}/{min_success} successful responses."
        )
    return FanoutResult(
        calls=calls,
        panel_wall_latency_ms=panel_wall,
        total_model_call_latency_ms=sum(call.latency_ms for call in calls),
        max_model_latency_ms=max((call.latency_ms for call in calls), default=0),
        min_successful_responses=min_success,
        quorum_met=quorum_met,
        timed_out=timed_out,
        warnings=warnings,
    )
