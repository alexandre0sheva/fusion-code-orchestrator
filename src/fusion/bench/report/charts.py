"""Inline SVG charts for the HTML report: plain strings, no library, no external asset.

Marks follow one spec: 2px lines, markers of at least 8px with a 2px ring in the surface colour,
bars at most 24px thick with a 4px rounded data end, hairline recessive grids. Colour carries
identity by arm slot (fixed order, never cycled) and every mark also has a direct label or a
legend entry, and a tooltip: ``data-tip`` holds the lines it shows (the first is the heading).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

__all__ = [
    "BarGroup",
    "Dot",
    "SpanBar",
    "bar_chart",
    "scatter_chart",
    "slot_class",
    "timeline_chart",
]

MAX_SLOTS = 8
SHAPES = ("circle", "square", "diamond", "triangle", "circle", "square", "diamond", "triangle")


def slot_class(index: int) -> str:
    """CSS class of an arm's colour slot; arms past the eighth share a neutral one."""
    return f"c{index + 1}" if index < MAX_SLOTS else "cx"


def _num(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Round tick values spanning ``low``..``high``."""
    if high <= low:
        high = low + 1.0
    raw = (high - low) / max(count, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.ceil(low / step - 1e-9) * step
    ticks: list[float] = []
    value = first
    while value <= high + step * 1e-9:
        ticks.append(round(value, 10))
        value += step
    return ticks


def tick_label(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 100:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:g}"
    return f"{value:.3g}"


def _tip(lines: Sequence[str]) -> str:
    return escape("\n".join(lines), quote=True)


def _marker(shape: str, x: float, y: float, cls: str, tip: str, index: int) -> str:
    """A marker of at least 8px with the 2px surface ring, inside a 24px hit area."""
    r = 5.0
    ring = 'stroke-width="2" class="mk ' + cls + '"'
    if shape == "square":
        body = f'<rect x="{_num(x - r)}" y="{_num(y - r)}" width="{_num(2 * r)}" height="{_num(2 * r)}" rx="1" {ring}/>'
    elif shape == "diamond":
        d = r * 1.45
        pts = f"{_num(x)},{_num(y - d)} {_num(x + d)},{_num(y)} {_num(x)},{_num(y + d)} {_num(x - d)},{_num(y)}"
        body = f'<polygon points="{pts}" {ring}/>'
    elif shape == "triangle":
        d = r * 1.35
        pts = f"{_num(x)},{_num(y - d)} {_num(x + d)},{_num(y + d * 0.8)} {_num(x - d)},{_num(y + d * 0.8)}"
        body = f'<polygon points="{pts}" {ring}/>'
    else:
        body = f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{_num(r)}" {ring}/>'
    return (
        f'<g class="pt" tabindex="0" data-tip="{tip}" data-arm="{index}">'
        f'<circle class="hit" cx="{_num(x)}" cy="{_num(y)}" r="14"/>{body}</g>'
    )


# ------------------------------------------------------------------------------- scatter


@dataclass(frozen=True)
class Dot:
    label: str
    slot: int
    x: float
    y: float
    x_low: float | None = None
    x_high: float | None = None
    y_low: float | None = None
    y_high: float | None = None
    frontier: bool = False
    tip: tuple[str, ...] = ()


def scatter_chart(
    dots: Sequence[Dot],
    *,
    x_label: str,
    y_label: str,
    x_format: str = "{:g}",
    title: str,
    desc: str,
) -> str:
    """Arms as points with interval whiskers; the frontier is joined by a 2px line."""
    width, height = 640, 380
    left, right, top, bottom = 62, 30, 14, 52
    if not dots:
        return ""
    xs = [v for d in dots for v in (d.x, d.x_low, d.x_high) if v is not None]
    ys = [v for d in dots for v in (d.y, d.y_low, d.y_high) if v is not None]
    x_lo, x_hi = 0.0, max(xs) * 1.12 if max(xs) > 0 else 1.0
    pad = max((max(ys) - min(ys)) * 0.15, 0.04)
    y_lo, y_hi = max(0.0, min(ys) - pad), min(1.0, max(ys) + pad)
    if y_hi - y_lo < 0.1:
        y_lo, y_hi = max(0.0, y_lo - 0.05), min(1.0, y_hi + 0.05)

    def px(value: float) -> float:
        return left + (value - x_lo) / (x_hi - x_lo) * (width - left - right)

    def py(value: float) -> float:
        return top + (1 - (value - y_lo) / (y_hi - y_lo)) * (height - top - bottom)

    out = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title, quote=True)}"'
        f' class="chart"><title>{escape(title)}</title><desc>{escape(desc)}</desc>'
    ]
    for tick in nice_ticks(y_lo, y_hi):
        y = py(tick)
        out.append(
            f'<line class="grid" x1="{left}" x2="{width - right}" y1="{_num(y)}" y2="{_num(y)}"/>'
            f'<text class="tick" x="{left - 8}" y="{_num(y + 4)}" text-anchor="end">{tick_label(tick)}</text>'
        )
    for tick in nice_ticks(x_lo, x_hi):
        x = px(tick)
        out.append(
            f'<line class="grid" x1="{_num(x)}" x2="{_num(x)}" y1="{top}" y2="{height - bottom}"/>'
            f'<text class="tick" x="{_num(x)}" y="{height - bottom + 18}" text-anchor="middle">'
            f"{escape(x_format.format(tick))}</text>"
        )
    out.append(
        f'<line class="axis" x1="{left}" x2="{width - right}" y1="{height - bottom}" y2="{height - bottom}"/>'
        f'<line class="axis" x1="{left}" x2="{left}" y1="{top}" y2="{height - bottom}"/>'
        f'<text class="axis-title" x="{(left + width - right) / 2}" y="{height - 8}" text-anchor="middle">{escape(x_label)}</text>'
        f'<text class="axis-title" transform="translate(14 {(top + height - bottom) / 2}) rotate(-90)" text-anchor="middle">{escape(y_label)}</text>'
    )
    joined = sorted((d for d in dots if d.frontier), key=lambda d: (d.x, -d.y))
    if len(joined) >= 2:
        pts = " ".join(f"{_num(px(d.x))},{_num(py(d.y))}" for d in joined)
        out.append(f'<polyline class="frontier" points="{pts}"/>')
    for d in dots:
        cls = slot_class(d.slot)
        x, y = px(d.x), py(d.y)
        if d.y_low is not None and d.y_high is not None:
            out.append(
                f'<line class="whisker {cls}" x1="{_num(x)}" x2="{_num(x)}" y1="{_num(py(d.y_low))}" y2="{_num(py(d.y_high))}"/>'
            )
        if d.x_low is not None and d.x_high is not None:
            out.append(
                f'<line class="whisker {cls}" x1="{_num(px(d.x_low))}" x2="{_num(px(d.x_high))}" y1="{_num(y)}" y2="{_num(y)}"/>'
            )
    boxes: list[tuple[float, float, float, float]] = [  # markers are obstacles for labels
        (px(d.x) - 9, px(d.x) + 9, py(d.y) - 9, py(d.y) + 9) for d in dots
    ]
    for index, d in enumerate(dots):
        cls = slot_class(d.slot)
        x, y = px(d.x), py(d.y)
        out.append(
            _marker(SHAPES[d.slot % len(SHAPES)], x, y, cls, _tip(d.tip or (d.label,)), index)
        )
    for d in dots:
        x, y = px(d.x), py(d.y)
        text_w = 6.4 * len(d.label)
        chosen: tuple[float, float, str] | None = None
        for dy in (-10.0, 10.0, -24.0, 24.0, -38.0, 38.0):
            for side in ("start", "end"):
                lx = x + 11 if side == "start" else x - 11
                x0, x1 = (lx, lx + text_w) if side == "start" else (lx - text_w, lx)
                y0, y1 = y + dy - 7, y + dy + 7
                clear = (
                    x0 >= left
                    and x1 <= width - 4
                    and y0 >= top
                    and y1 <= height - bottom
                    and not any(a < x1 and x0 < b and c < y1 and y0 < e for a, b, c, e in boxes)
                )
                if clear:
                    chosen = (lx, y + dy, side)
                    boxes.append((x0, x1, y0, y1))
                    break
            if chosen:
                break
        if chosen is None:  # nowhere is free: keep it beside its marker
            chosen = (x + 11, y - 10, "start")
        lx, ly, anchor = chosen
        out.append(
            f'<text class="label" x="{_num(lx)}" y="{_num(ly + 4)}" text-anchor="{anchor}">{escape(d.label)}</text>'
        )
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------------- bars


@dataclass(frozen=True)
class BarGroup:
    label: str
    bars: tuple[tuple[str, int, float, float | None, float | None, tuple[str, ...]], ...]
    # each bar: (arm label, slot, value, ci low, ci high, tooltip lines)


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """A bar square at its baseline (left) with a rounded data end (right)."""
    if w <= r:
        return f"M{_num(x)},{_num(y)}h{_num(max(w, 0.5))}v{_num(h)}h-{_num(max(w, 0.5))}z"
    r = min(r, h / 2)
    return (
        f"M{_num(x)},{_num(y)}h{_num(w - r)}a{_num(r)},{_num(r)} 0 0 1 {_num(r)},{_num(r)}"
        f"v{_num(h - 2 * r)}a{_num(r)},{_num(r)} 0 0 1 -{_num(r)},{_num(r)}h-{_num(w - r)}z"
    )


def bar_chart(
    groups: Sequence[BarGroup],
    *,
    maximum: float,
    value_format: str = "{:.2f}",
    title: str,
    desc: str,
) -> str:
    """Grouped horizontal bars from one baseline; the value sits at the bar's tip."""
    if not groups:
        return ""
    bar_h, gap, group_gap = 14.0, 2.0, 16.0
    left, right, top = 120, 70, 8
    width = 640
    plot = width - left - right
    rows: list[str] = []
    y = float(top)
    for group in groups:
        rows.append(
            f'<text class="group" x="{left - 10}" y="{_num(y + 11)}" text-anchor="end">{escape(group.label)}</text>'
        )
        for arm, slot, value, low, high, tip in group.bars:
            w = max(value / maximum * plot, 0.0)
            cls = slot_class(slot)
            ci = ""
            if low is not None and high is not None:
                cy = y + bar_h / 2
                ci = (
                    f'<line class="whisker {cls}" x1="{_num(left + low / maximum * plot)}" x2="{_num(left + high / maximum * plot)}"'
                    f' y1="{_num(cy)}" y2="{_num(cy)}"/>'
                )
            rows.append(
                f'<g class="pt" tabindex="0" data-tip="{_tip(tip or (arm,))}">'
                f'<rect class="hit" x="{left}" y="{_num(y - gap / 2)}" width="{plot + right - 6}" height="{_num(bar_h + gap)}"/>'
                f'<path class="bar {cls}" d="{_bar_path(left, y, w, bar_h)}"/>{ci}'
                f'<text class="value" x="{_num(left + max(w, (high or 0.0) / maximum * plot) + 6)}" y="{_num(y + 11)}">{escape(value_format.format(value))}</text></g>'
            )
            y += bar_h + gap
        y += group_gap
    height = int(y)
    axis = f'<line class="axis" x1="{left}" x2="{left}" y1="{top}" y2="{height - int(group_gap) + 2}"/>'
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title, quote=True)}" class="chart bars">'
        f"<title>{escape(title)}</title><desc>{escape(desc)}</desc>{axis}{''.join(rows)}</svg>"
    )


# ------------------------------------------------------------------------------ timeline


@dataclass(frozen=True)
class SpanBar:
    arm: str
    slot: int
    lane: int  # row within the arm's block
    start: float
    end: float
    stage: int  # colour slot of the stage
    tip: tuple[str, ...] = ()


def timeline_chart(
    blocks: Sequence[tuple[str, int, Sequence[SpanBar]]],
    *,
    horizon: float,
    title: str,
    desc: str,
) -> str:
    """One block per arm: each call is a bar on the run's own clock, one row per call."""
    if not blocks or horizon <= 0:
        return ""
    row_h, label_h, block_gap = 12.0, 18.0, 14.0
    left, right, top, bottom = 24, 30, 6, 34
    width = 640
    plot = width - left - right

    def px(value: float) -> float:
        return left + value / horizon * plot

    out: list[str] = []
    y = float(top)
    for arm, _slot, spans in blocks:
        out.append(f'<text class="group" x="{left}" y="{_num(y + 12)}">{escape(arm)}</text>')
        y += label_h
        lanes = max((s.lane for s in spans), default=-1) + 1
        for span in spans:
            w = max(px(span.end) - px(span.start), 3.0)
            out.append(
                f'<g class="pt" tabindex="0" data-tip="{_tip(span.tip)}">'
                f'<rect class="hit" x="{_num(px(span.start) - 2)}" y="{_num(y + span.lane * row_h - 2)}" width="{_num(w + 4)}" height="{_num(row_h)}"/>'
                f'<rect class="span {slot_class(span.stage)}" x="{_num(px(span.start))}" y="{_num(y + span.lane * row_h)}"'
                f' width="{_num(w)}" height="8" rx="2"/></g>'
            )
        y += lanes * row_h + block_gap
    height = int(y + bottom - block_gap)
    axis = [
        f'<line class="axis" x1="{left}" x2="{width - right}" y1="{height - bottom}" y2="{height - bottom}"/>'
    ]
    for tick in nice_ticks(0, horizon):
        x = px(tick)
        axis.append(
            f'<line class="grid" x1="{_num(x)}" x2="{_num(x)}" y1="{top}" y2="{height - bottom}"/>'
            f'<text class="tick" x="{_num(x)}" y="{height - bottom + 16}" text-anchor="middle">{tick_label(tick)}s</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title, quote=True)}" class="chart timeline">'
        f"<title>{escape(title)}</title><desc>{escape(desc)}</desc>{''.join(axis)}{''.join(out)}</svg>"
    )
