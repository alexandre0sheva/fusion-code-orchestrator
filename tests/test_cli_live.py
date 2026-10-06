"""The live run view: a panel on a terminal, plain lines anywhere else, never in the cost."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from fusion.cli.common import make_tools, run_tool
from fusion.cli.live import RunView, tokens, usd
from fusion.mcp_server.schemas import FusionAskInput
from fusion.orchestration.ledger import CallRecord


def record(
    stage: str = "panel",
    alias: str = "model-a",
    *,
    cost: float | None = 0.0123,
    ok: bool = True,
    **extra: object,
) -> CallRecord:
    fields: dict[str, object] = {
        "stage": stage,
        "model_alias": alias,
        "provider": "p",
        "model_id": f"{alias}-1",
        "input_tokens": 1200,
        "output_tokens": 300,
        "cost_usd": cost,
        "cost_known": cost is not None,
        "latency_ms": 2500.0,
        "ok": ok,
        "status": "success" if ok else "failed",
        "error": None if ok else "TimeoutError: Timed out after 30.0s",
    }
    fields.update(extra)
    return CallRecord(**fields)  # type: ignore[arg-type]


def plain_view(**kwargs: object) -> tuple[RunView, StringIO]:
    out = StringIO()
    return RunView(console=Console(file=out, force_terminal=False, width=200), **kwargs), out  # type: ignore[arg-type]


def snapshot(view: RunView) -> str:
    console = Console(file=StringIO(), force_terminal=False, width=110, record=True)
    console.print(view)
    return console.export_text()


async def test_without_a_terminal_every_event_is_one_plain_line() -> None:
    view, out = plain_view()
    assert not view.live
    await view.message("panel: asking 2 models")
    view.call_started("panel", "model-a")
    view.call_finished(record())
    view.call_started("panel", "model-b")
    view.call_finished(record(alias="model-b", ok=False, cost=0.0))
    assert out.getvalue().splitlines() == [
        "panel: asking 2 models",
        "  ok    panel      model-a            1,200 in / 300 out  $0.0123  2.5s",
        "  FAIL  panel      model-b            TimeoutError: Timed out after 30.0s",
    ]


async def test_a_message_with_markup_characters_is_printed_as_text() -> None:
    view, out = plain_view()
    await view.message("[bold]not markup[/bold] [red")
    assert out.getvalue().strip() == "[bold]not markup[/bold] [red"


async def test_quiet_prints_nothing_and_a_terminal_is_not_used_when_quiet() -> None:
    out = StringIO()
    console = Console(file=out, force_terminal=True, width=100)
    view = RunView(console=console, quiet=True)
    assert not view.live
    with view:
        await view.message("anything")
        view.call_finished(record())
    assert out.getvalue() == ""


def test_totals_leave_out_running_and_shadow_calls() -> None:
    view, _ = plain_view()
    view.call_started("panel", "running-one")
    view.call_finished(record(alias="done", cost=0.01))
    view.call_finished(record("shadow_baseline", "opus", cost=5.0))
    cost, known, tokens_in, tokens_out = view.totals()
    assert cost == pytest.approx(0.01) and known and (tokens_in, tokens_out) == (1200, 300)


def test_an_unknown_price_is_marked_not_counted_as_zero() -> None:
    view, _ = plain_view()
    view.call_finished(record(alias="known", cost=0.01))
    view.call_finished(record(alias="mystery", cost=None))
    cost, known, _, _ = view.totals()
    assert cost == pytest.approx(0.01) and not known
    assert "cost $0.0100+" in snapshot(view)
    assert usd(None) == "?" and usd(0.5) == "$0.5000" and usd(12.3456) == "$12.35"
    assert tokens(None, None) == "-" and tokens(1500, 20) == "1,500 in / 20 out"


def test_the_panel_shows_a_row_per_call_and_the_baseline_estimate() -> None:
    view, _ = plain_view()
    view.stage_text = "panel: asking 3 models"
    view.call_started("panel", "still-working")
    view.call_finished(record(alias="fast", cost=0.001))
    view.call_finished(record(alias="slow", ok=False, cost=0.0))
    view.call_finished(record("shadow_baseline", "opus", cost=4.0))
    text = snapshot(view)
    assert "panel: asking 3 models" in text
    assert "✓" in text and "✗" in text
    assert "still-working" in text and "fast" in text and "slow" in text
    assert "opus" in text and "(not counted)" in text
    assert "baseline estimate for the same tokens $" in text
    footer = next(line for line in text.splitlines() if line.strip().startswith("cost"))
    assert "cost $0.0010" in footer  # the shadow call's $4 is not in it


async def test_a_terminal_gets_the_live_panel() -> None:
    out = StringIO()
    console = Console(file=out, force_terminal=True, width=100)
    view = RunView(console=console)
    assert view.live
    with view:
        await view.message("synthesizing")
        view.call_started("synthesis", "judge")
        view.call_finished(record("synthesis", "judge"))
    drawn = out.getvalue()
    assert "synthesizing" in drawn and "judge" in drawn  # drawn while it ran
    assert "\x1b[?25h" in drawn  # the cursor is given back
    assert drawn.endswith("\x1b[2K")  # and the transient panel is erased, leaving the screen clean


def test_a_real_run_fills_the_view_through_the_pipeline_hooks() -> None:
    out = StringIO()
    tools = make_tools(None, True)
    request = FusionAskInput(prompt="How should I retry a failed HTTP call without hammering it?")
    output = run_tool(
        tools,
        lambda t: t.fusion_ask(request),
        view_console=Console(file=out, force_terminal=False, width=200),
    )
    lines = out.getvalue().splitlines()
    assert output["run_id"]
    panel = [ln for ln in lines if ln.startswith("  ok    panel")]
    assert len(panel) == 2  # the default panel has two models
    assert any(ln.startswith("  ok    synthesis") for ln in lines)
    assert lines.index("panel: asking 2 models") < lines.index(panel[0])
    assert "synthesizing" in lines
