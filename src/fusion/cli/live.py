"""What a run looks like while it works: one row per model call, and a running cost.

On a terminal a Rich ``Live`` panel shows the current stage, a row per call (spinner, then a tick
or a cross, with tokens, cost and latency) and the running cost beside what the same tokens would
cost on the baseline model. Anywhere else (a pipe, a file, CI) it prints one plain line per event,
which is stable and greppable. Both go to stderr, so stdout carries only the result.

The view listens through ``fusion.orchestration.progress``: stage messages arrive as a progress
sink, calls as a call observer. Shadow-baseline calls are shown but never counted in the cost.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from fusion.orchestration.progress import call_observer, progress_sink

if TYPE_CHECKING:
    from fusion.orchestration.ledger import CallRecord

SHADOW_STAGES = frozenset({"shadow_baseline", "shadow_judge"})


def usd(value: float | None) -> str:
    """A cost for a table cell; unknown is ``?`` (never a made-up zero)."""
    if value is None:
        return "?"
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def tokens(in_tokens: int | None, out_tokens: int | None) -> str:
    if in_tokens is None and out_tokens is None:
        return "-"
    return f"{in_tokens or 0:,} in / {out_tokens or 0:,} out"


@dataclass
class _Row:
    stage: str
    alias: str
    started: float = field(default_factory=time.monotonic)
    state: str = "running"  # running, ok, failed or cancelled
    in_tokens: int | None = None
    out_tokens: int | None = None
    cost: float | None = None
    latency_ms: float | None = None
    error: str | None = None
    cache_hit: bool = False

    @property
    def shadow(self) -> bool:
        return self.stage in SHADOW_STAGES


class RunView:
    """A context manager that shows the run inside it; also the observer and the progress sink."""

    def __init__(self, *, console: Console, quiet: bool = False) -> None:
        self.console = console
        self.quiet = quiet
        self.live = console.is_terminal and not quiet
        self.rows: list[_Row] = []
        self.stage_text = "starting"
        self._began = time.monotonic()
        self._estimate: float | None = None
        self._stack = ExitStack()
        self._display: Live | None = None

    # -- the context -------------------------------------------------------------------------

    def __enter__(self) -> RunView:
        self._began = time.monotonic()
        self._stack.enter_context(progress_sink(self.message))
        self._stack.enter_context(call_observer(self))
        if self.live:
            self._display = Live(self, console=self.console, refresh_per_second=10, transient=True)
            self._stack.enter_context(self._display)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stack.close()

    # -- events ------------------------------------------------------------------------------

    async def message(self, text: str) -> None:
        """A stage message from the pipeline ("panel: asking 3 models", "synthesizing")."""
        self.stage_text = text
        self._line(escape(text))

    def call_started(self, stage: str, alias: str) -> None:
        self.rows.append(_Row(stage, alias))

    def call_finished(self, record: CallRecord) -> None:
        row = next(
            (
                r
                for r in self.rows
                if r.state == "running" and (r.stage, r.alias) == (record.stage, record.model_alias)
            ),
            None,
        )
        if row is None:  # a call that never started (no provider configured)
            row = _Row(record.stage, record.model_alias)
            self.rows.append(row)
        row.state = (
            "ok" if record.ok else ("cancelled" if record.status == "cancelled" else "failed")
        )
        row.in_tokens, row.out_tokens = record.input_tokens, record.output_tokens
        row.cost = record.cost_usd if record.cost_known else None
        row.latency_ms = record.latency_ms
        row.error = record.error
        row.cache_hit = record.cache_hit
        self._estimate = self._baseline_estimate()
        self._line(self._plain(row))

    # -- numbers -----------------------------------------------------------------------------

    def totals(self) -> tuple[float, bool, int, int]:
        """Cost so far, whether every price was known, and tokens in and out (shadow excluded)."""
        cost, known, tokens_in, tokens_out = 0.0, True, 0, 0
        for row in self.rows:
            if row.shadow or row.state == "running":
                continue
            if row.cost is None:
                known = False
            else:
                cost += row.cost
            tokens_in += row.in_tokens or 0
            tokens_out += row.out_tokens or 0
        return cost, known, tokens_in, tokens_out

    def _baseline_estimate(self) -> float | None:
        """What the tokens so far would cost on the baseline model (None when it cannot be told)."""
        from fusion.telemetry.cost import UsageSummary, compare_to_baseline

        cost, known, tokens_in, tokens_out = self.totals()
        if not tokens_in and not tokens_out:
            return None
        try:
            comparison = compare_to_baseline(
                usage=UsageSummary(
                    total_input_tokens=tokens_in,
                    total_output_tokens=tokens_out,
                    total_tokens=tokens_in + tokens_out,
                    fusion_wall_latency_ms=0,
                ),
                fusion_total_cost_usd=cost,
                fusion_cost_known=known,
            )
        except Exception:  # an estimate that cannot be made is left out, never a crash
            return None
        return comparison.baseline_estimated_cost_usd

    # -- plain lines (not a terminal) -------------------------------------------------------

    def _line(self, text: str) -> None:
        if self.live or self.quiet:
            return
        self.console.print(text, soft_wrap=True, highlight=False, markup=True)

    @staticmethod
    def _plain(row: _Row) -> str:
        label = {"ok": "ok  ", "failed": "FAIL", "cancelled": "stop"}.get(row.state, "....")
        head = f"  {label}  {row.stage:<10} {row.alias:<18}"
        if row.state == "ok":
            seconds = (row.latency_ms or 0.0) / 1000
            cached = "  (cached)" if row.cache_hit else ""
            return escape(
                f"{head} {tokens(row.in_tokens, row.out_tokens)}  {usd(row.cost)}  "
                f"{seconds:.1f}s{cached}"
            )
        return escape(f"{head} {row.error or row.state}")

    # -- the live panel ----------------------------------------------------------------------

    def __rich__(self) -> RenderableType:
        elapsed = time.monotonic() - self._began
        table = Table.grid(padding=(0, 2))
        for row in self.rows:
            mark: RenderableType
            if row.state == "running":
                mark = Spinner("dots")
                seconds = f"{time.monotonic() - row.started:.1f}s"
                detail = Text("")
            elif row.state == "ok":
                mark = Text("✓", style="green")
                seconds = f"{(row.latency_ms or 0.0) / 1000:.1f}s"
                detail = Text(f"{tokens(row.in_tokens, row.out_tokens)}   {usd(row.cost)}")
            else:
                mark = Text("✗", style="red")
                seconds = f"{(row.latency_ms or 0.0) / 1000:.1f}s"
                detail = Text(
                    row.error or row.state, style="red", overflow="ellipsis", no_wrap=True
                )
            style = "dim" if row.shadow else ""
            table.add_row(
                mark,
                Text(row.stage + (" (not counted)" if row.shadow else ""), style=style),
                Text(row.alias, style=style),
                detail,
                Text(seconds, style="dim"),
            )
        cost, known, _tokens_in, _tokens_out = self.totals()
        footer = f"cost {usd(cost) if known else usd(cost) + '+'}"
        if self._estimate is not None:
            footer += f"   ·   baseline estimate for the same tokens {usd(self._estimate)}"
        return Group(
            Text.from_markup(f"[bold]{escape(self.stage_text)}[/bold]   [dim]{elapsed:.0f}s[/dim]"),
            table,
            Text(footer, style="dim"),
        )
