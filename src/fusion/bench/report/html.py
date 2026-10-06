"""One self-contained HTML file: inline SVG charts, inline CSS and JS, no external asset.

Light and dark follow the viewer's setting (a button overrides it). Every chart has a table
beside or below it with the same numbers, and every mark a tooltip that shows on hover and focus.
Screenshots of the most different answers are embedded as ``data:`` images.
"""

from __future__ import annotations

import base64
from html import escape
from pathlib import Path

from fusion.bench.report.charts import (
    BarGroup,
    Dot,
    SpanBar,
    bar_chart,
    scatter_chart,
    slot_class,
    timeline_chart,
)
from fusion.bench.report.data import ExampleSide, Report, screenshot_path
from fusion.bench.report.fmt import (
    CLAIM_TEXT,
    OUTCOME_SYMBOL,
    OUTCOME_WORD,
    comparison_for,
    interval_text,
    methodology,
    money,
    pct,
    seconds,
)
from fusion.bench.stats import CLAIMS, ArmStats

__all__ = ["cost_quality_svg", "render_html"]

MAX_IMAGE_BYTES = 400_000  # one embedded screenshot
MAX_IMAGE_TOTAL = 3_000_000  # all of them: the file stays a file one can email

_CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#6f6d68;
--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--good:#006300;--bad:#b02a2a;--warn:#8a5a00;
--c1:#2a78d6;--c2:#eb6834;--c3:#1baf7a;--c4:#eda100;--c5:#e87ba4;--c6:#008300;--c7:#4a3aa7;--c8:#e34948;--cx:#898781}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--muted:#a3a198;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--good:#0ca30c;--bad:#ec7a7a;--warn:#fab219;
--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--c5:#d55181;--c6:#008300;--c7:#9085e9;--c8:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#a3a198;
--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--good:#0ca30c;--bad:#ec7a7a;--warn:#fab219;
--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--c5:#d55181;--c6:#008300;--c7:#9085e9;--c8:#e66767}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:24px 16px 64px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between}
h1{font-size:1.5rem;margin:0}h2{font-size:1.15rem;margin:36px 0 8px}h3{font-size:1rem;margin:20px 0 6px}
p,li{color:var(--ink2)}.sub{color:var(--ink2);margin:4px 0 0}
button{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--axis);border-radius:8px;padding:4px 12px;cursor:pointer}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:16px;margin:12px 0}
.note{border-left:3px solid var(--warn);background:var(--surface);padding:8px 12px;margin:8px 0;color:var(--ink2);border-radius:0 8px 8px 0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,440px),1fr));gap:12px}
table{border-collapse:collapse;width:100%;font-size:.85rem;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
th,td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--grid);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--ink2);font-weight:600}
caption{caption-side:top;text-align:left;color:var(--ink2);padding:0 0 6px}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:baseline}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;margin:6px 0;font-size:.85rem;color:var(--ink2)}
.verdicts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:8px}
.v{border:1px solid var(--ring);border-radius:10px;padding:8px 12px;background:var(--surface)}
.v b{display:block}.v .yes{color:var(--good)}.v .no{color:var(--bad)}.v .blocked{color:var(--warn)}.v .inconclusive{color:var(--ink2)}
.reason{font-size:.8rem;color:var(--ink2);margin:2px 0 0}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
svg text{font:12px system-ui,-apple-system,"Segoe UI",sans-serif;fill:var(--ink2)}
svg .tick{fill:var(--muted)}svg .group,svg .label{fill:var(--ink)}svg .value{fill:var(--ink)}svg .axis-title{fill:var(--ink2)}
svg .grid{stroke:var(--grid);stroke-width:1}svg .axis{stroke:var(--axis);stroke-width:1}
svg .frontier{fill:none;stroke:var(--muted);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .whisker{stroke-width:2;stroke-linecap:round;opacity:.55}
svg .hit{fill:transparent}svg .pt{outline:none;cursor:default}
svg .mk{stroke:var(--surface)}svg .pt:hover .mk,svg .pt:focus .mk{stroke:var(--ink)}
svg .pt:hover .bar,svg .pt:focus .bar,svg .pt:hover .span,svg .pt:focus .span{filter:brightness(1.12);outline:none}
"""
# Fills and strokes per colour slot, generated so the eight slots cannot drift apart.
_CSS += "".join(
    f"svg .mk.c{n},svg .bar.c{n},svg .span.c{n}{{fill:var(--c{n})}}svg .whisker.c{n}{{stroke:var(--c{n})}}\n"
    for n in range(1, 9)
)
_CSS += """svg .mk.cx,svg .bar.cx,svg .span.cx{fill:var(--cx)}svg .whisker.cx{stroke:var(--cx)}
svg .mk{stroke:var(--surface);stroke-width:2}
.bg1{background:var(--c1)}.bg2{background:var(--c2)}.bg3{background:var(--c3)}.bg4{background:var(--c4)}
.bg5{background:var(--c5)}.bg6{background:var(--c6)}.bg7{background:var(--c7)}.bg8{background:var(--c8)}.bgx{background:var(--cx)}
#tip{position:fixed;z-index:9;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--axis);
border-radius:8px;padding:6px 10px;font-size:.8rem;max-width:300px;box-shadow:0 2px 8px var(--ring);display:none}
#tip b{display:block;font-size:.9rem}#tip span{display:block;color:var(--ink2)}
img.shot{max-width:100%;border:1px solid var(--ring);border-radius:8px;margin:4px 0}
.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px}
code{font:.85em ui-monospace,SFMono-Regular,Menlo,monospace}
dl{margin:0;display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;font-size:.85rem}dt{color:var(--ink2)}dd{margin:0;overflow-wrap:anywhere}
@media (max-width:600px){main{padding:16px 12px 48px}}
"""

_JS = """
(function(){
var tip=document.getElementById('tip');
function show(el,x,y){var lines=(el.getAttribute('data-tip')||'').split('\\n');tip.textContent='';
lines.forEach(function(t,i){var n=document.createElement(i===0?'b':'span');n.textContent=t;tip.appendChild(n);});
tip.style.display='block';var w=tip.offsetWidth,h=tip.offsetHeight;
tip.style.left=Math.max(8,Math.min(x+14,window.innerWidth-w-8))+'px';tip.style.top=Math.max(8,Math.min(y+14,window.innerHeight-h-8))+'px';}
function hide(){tip.style.display='none';}
document.querySelectorAll('.pt').forEach(function(el){
el.addEventListener('pointermove',function(e){show(el,e.clientX,e.clientY);});
el.addEventListener('pointerleave',hide);
el.addEventListener('focus',function(){var r=el.getBoundingClientRect();show(el,r.left+r.width/2,r.top+r.height/2);});
el.addEventListener('blur',hide);});
var btn=document.getElementById('theme');
btn.addEventListener('click',function(){var root=document.documentElement;
var dark=root.getAttribute('data-theme')?root.getAttribute('data-theme')==='dark':window.matchMedia('(prefers-color-scheme: dark)').matches;
root.setAttribute('data-theme',dark?'light':'dark');});
})();
"""


def _e(value: object) -> str:
    return escape(str(value))


def _slot(report: Report, arm: str) -> int:
    names = [a.arm for a in report.stats.arms]
    return names.index(arm) if arm in names else len(names)


def _legend(report: Report) -> str:
    items = "".join(
        f'<span><i class="sw bg{slot_class(i)[1:]}"></i>{_e(a.arm)}</span>'
        for i, a in enumerate(report.stats.arms)
    )
    return f'<div class="legend" aria-label="Arms">{items}</div>'


def _verdict_cards(report: Report) -> str:
    stats = report.stats
    blocks: list[str] = []
    for arm in stats.arms:
        cmp = comparison_for(stats.comparisons, arm.arm)
        if cmp is None:
            continue
        cards = "".join(
            f'<div class="v"><b class="{v.outcome}"><span aria-hidden="true">{OUTCOME_SYMBOL[v.outcome]}</span> '
            f"{_e(CLAIM_TEXT[v.claim].capitalize())}: {OUTCOME_WORD[v.outcome]}</b>"
            f'<p class="reason">{_e(v.reason)}</p></div>'
            for v in cmp.verdicts
        )
        blocks.append(
            f'<div class="card"><h3><i class="sw bg{slot_class(_slot(report, arm.arm))[1:]}"></i>'
            f"{_e(arm.arm)} vs {_e(stats.baseline)} <small>({cmp.n_tasks} paired tasks)</small></h3>"
            f'<div class="verdicts">{cards}</div></div>'
        )
    return "".join(blocks)


def _arm_tip(a: ArmStats) -> tuple[str, ...]:
    return (
        a.arm,
        f"quality {interval_text(a.mean_quality)}",
        f"$/task {interval_text(a.cost_per_task, '.4f')}",
        f"$/solved {interval_text(a.cost_per_solved, '.4f')}",
        f"seconds p50 {seconds(a.seconds_p50)}, p90 {seconds(a.seconds_p90)}",
    )


def _pareto_charts(report: Report) -> str:
    stats = report.stats
    cost_dots: list[Dot] = []
    latency_dots: list[Dot] = []
    for a in stats.arms:
        if a.mean_quality is None:
            continue
        slot = _slot(report, a.arm)
        q = a.mean_quality
        if a.cost_per_task is not None:
            c = a.cost_per_task
            cost_dots.append(
                Dot(a.arm, slot, c.estimate, q.estimate, c.low, c.high, q.low, q.high,
                    a.arm in stats.pareto_cost, _arm_tip(a))
            )  # fmt: skip
        if a.seconds_p50 is not None:
            latency_dots.append(
                Dot(a.arm, slot, a.seconds_p50, q.estimate, None, a.seconds_p90, q.low, q.high,
                    a.arm in stats.pareto_latency, _arm_tip(a))
            )  # fmt: skip
    cost = scatter_chart(
        cost_dots,
        x_label="Cost per task (USD); bars are 95% intervals",
        y_label="Mean quality",
        x_format="${:g}",
        title="Quality against cost per task",
        desc="One point per arm. Up and to the left is better. The line joins the arms no other arm beats.",
    )
    latency = scatter_chart(
        latency_dots,
        x_label="Median seconds to complete (bar reaches p90)",
        y_label="Mean quality",
        x_format="{:g}s",
        title="Quality against median latency",
        desc="One point per arm. Up and to the left is better. The line joins the arms no other arm beats.",
    )
    out = '<div class="grid2">'
    out += f'<div class="card"><h3>Quality and cost</h3>{cost}</div>' if cost else ""
    out += f'<div class="card"><h3>Quality and speed</h3>{latency}</div>' if latency else ""
    return out + "</div>"


def _category_chart(report: Report) -> str:
    stats = report.stats
    groups = []
    for category, arms in stats.by_category.items():
        bars = tuple(
            (
                a.arm,
                _slot(report, a.arm),
                a.mean_quality.estimate,
                a.mean_quality.low,
                a.mean_quality.high,
                (
                    f"{a.arm} on {category}",
                    f"quality {interval_text(a.mean_quality)}",
                    f"{a.n_tasks} tasks, solved {pct(a.solved_rate.estimate if a.solved_rate else None)}",
                    f"$/solved {interval_text(a.cost_per_solved, '.4f', bounds=False)}",
                ),
            )
            for a in arms
            if a.mean_quality is not None
        )
        if bars:
            groups.append(BarGroup(category, bars))
    chart = bar_chart(
        groups,
        maximum=1.0,
        title="Mean quality by category",
        desc="A bar per arm in each category, from 0 to 1, with a whisker for the 95% interval.",
    )
    return (
        f'<div class="card"><h3>Quality by category</h3>{_legend(report)}{chart}</div>'
        if chart
        else ""
    )


_STAGE_SLOTS = ("panel", "claims", "refine", "judge", "synthesis", "cascade", "other")


def _timeline(report: Report) -> str:
    if not report.timelines:
        return ""
    horizon = max((s.end_s for spans in report.timelines.values() for s in spans), default=0.0)
    seen: list[str] = []
    blocks = []
    for arm, spans in report.timelines.items():
        bars = []
        for lane, span in enumerate(spans):
            stage = span.stage if span.stage in _STAGE_SLOTS else "other"
            if stage not in seen:
                seen.append(stage)
            bars.append(
                SpanBar(
                    arm,
                    _slot(report, arm),
                    lane,
                    span.start_s,
                    span.end_s,
                    _STAGE_SLOTS.index(stage),
                    (
                        f"{span.model}: {span.stage}",
                        f"{span.start_s:.2f}s to {span.end_s:.2f}s ({span.end_s - span.start_s:.2f}s)"
                        + (", replayed from cache" if span.cache_hit else ""),
                        f"task {report.timeline_task.get(arm, '')}",
                    ),
                )
            )
        blocks.append(
            (f"{arm} (task {report.timeline_task.get(arm, '')})", _slot(report, arm), bars)
        )
    chart = timeline_chart(
        blocks,
        horizon=horizon,
        title="Call timeline of a typical run per arm",
        desc="Each call of the run with the median latency, on that run's own clock.",
    )
    legend = "".join(
        f'<span><i class="sw bg{slot_class(_STAGE_SLOTS.index(s))[1:]}"></i>{_e(s)}</span>'
        for s in seen
    )
    return (
        '<div class="card"><h3>Where the time goes</h3>'
        "<p>One run per arm (the one with the median latency): each bar is a model call. Calls that "
        "overlap ran at the same time; a gap is waiting.</p>"
        f'<div class="legend" aria-label="Stages"><span>Stage:</span>{legend}</div>{chart}</div>'
    )


def _arm_table(report: Report) -> str:
    stats = report.stats
    rows = ""
    for a in stats.arms:
        flags = " ".join(
            t
            for t, frontier in (
                ("cost frontier", stats.pareto_cost),
                ("speed frontier", stats.pareto_latency),
            )
            if a.arm in frontier
        )
        rows += (
            f'<tr><td><i class="sw bg{slot_class(_slot(report, a.arm))[1:]}"></i>{_e(a.arm)}</td>'
            f"<td>{a.n_tasks}</td><td>{_e(interval_text(a.mean_quality))}</td>"
            f"<td>{pct(a.solved_rate.estimate if a.solved_rate else None)}</td>"
            f"<td>{money(a.cost_per_task.estimate if a.cost_per_task else None)}</td>"
            f"<td>{money(a.cost_per_solved.estimate if a.cost_per_solved else None)}</td>"
            f"<td>{seconds(a.seconds_p50)}</td><td>{seconds(a.seconds_p90)}</td>"
            f"<td>{'-' if a.output_tokens_per_s is None else f'{a.output_tokens_per_s:.0f}'}</td>"
            f"<td>{'-' if a.tokens_per_solved is None else f'{a.tokens_per_solved:,.0f}'}</td>"
            f"<td>{'-' if a.quality_per_dollar is None else f'{a.quality_per_dollar:.1f}'}</td>"
            f"<td>{'-' if a.quality_per_minute is None else f'{a.quality_per_minute:.3f}'}</td>"
            f"<td>{money(a.eval_cost_per_item)}</td><td>{seconds(a.eval_seconds_per_item)}</td>"
            f"<td>{_e(flags) or '-'}</td></tr>"
        )
    head = (
        "Arm|Tasks|Quality (95% CI)|Solved|$/task|$/solved|p50|p90|Out tok/s|Tokens/solved|"
        "Quality/$|Quality/min|Eval $/item|Eval s/item|Frontier"
    )
    th = "".join(f"<th>{_e(h)}</th>" for h in head.split("|"))
    return (
        '<div class="scroll"><table><caption>Every arm, all tasks. Eval columns are what scoring and '
        "judging cost: shown beside, never inside, an arm's cost and time.</caption>"
        f"<thead><tr>{th}</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _comparison_table(report: Report) -> str:
    stats = report.stats
    out = ""
    for arm in stats.arms:
        rows = ""
        for cmp in (c for c in stats.comparisons if c.challenger == arm.arm):
            by_claim = {v.claim: v for v in cmp.verdicts}
            q = cmp.quality
            marks = "".join(
                f'<td title="{_e(by_claim[c].reason)}">{OUTCOME_SYMBOL[by_claim[c].outcome]} {OUTCOME_WORD[by_claim[c].outcome]}</td>'
                for c in CLAIMS
            )
            rows += (
                f"<tr><td>{_e(cmp.scope)}</td><td>{cmp.n_tasks}</td>"
                f"<td>{_e(interval_text(q.difference, '+.3f'))}</td><td>{q.wins}/{q.ties}/{q.losses}</td>"
                f"<td>{'-' if q.sign_test_p is None else f'{q.sign_test_p:.3f}'}</td>"
                f"<td>{'-' if q.effect_size is None else f'{q.effect_size:+.2f}'}</td>"
                f"<td>{_e(interval_text(cmp.cost_per_solved_ratio, '.2f'))}</td>"
                f"<td>{_e(interval_text(cmp.seconds_ratio, '.2f'))}</td>{marks}</tr>"
            )
        if not rows:
            continue
        head = "Scope|Tasks|Δ quality (95% CI)|W/T/L|Sign test p|Effect d|$/solved ratio|Seconds ratio|Cheaper|Faster|Not worse|Better"
        th = "".join(f"<th>{_e(h)}</th>" for h in head.split("|"))
        out += (
            f'<div class="scroll"><table><caption>{_e(arm.arm)} vs {_e(stats.baseline)} '
            f"(paired by task; hover a verdict for its reason)</caption>"
            f"<thead><tr>{th}</tr></thead><tbody>{rows}</tbody></table></div>"
        )
    return out


def _difficulty_table(report: Report) -> str:
    stats = report.stats
    if not stats.by_difficulty:
        return ""
    rows = ""
    for level, arms in stats.by_difficulty.items():
        for a in arms:
            rows += (
                f"<tr><td>{_e(level)}</td><td>{_e(a.arm)}</td><td>{a.n_tasks}</td>"
                f"<td>{_e(interval_text(a.mean_quality))}</td>"
                f"<td>{money(a.cost_per_solved.estimate if a.cost_per_solved else None)}</td></tr>"
            )
    th = "".join(
        f"<th>{h}</th>" for h in ("Difficulty", "Arm", "Tasks", "Quality (95% CI)", "$/solved")
    )
    return f'<div class="scroll"><table><caption>By difficulty</caption><thead><tr>{th}</tr></thead><tbody>{rows}</tbody></table></div>'


def _variance_table(report: Report) -> str:
    rows = "".join(
        f"<tr><td>{_e(a.arm)}</td><td>{'-' if a.repeat_sd is None else f'{a.repeat_sd:.3f}'}</td>"
        f"<td>{pct(a.flip_rate)}</td></tr>"
        for a in report.stats.arms
    )
    th = "".join(
        f"<th>{h}</th>" for h in ("Arm", "Mean sd across repeats", "Tasks whose solved flips")
    )
    return f'<div class="scroll"><table><caption>Variance across repeats</caption><thead><tr>{th}</tr></thead><tbody>{rows}</tbody></table></div>'


def _image(path: Path, budget: list[int]) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return "<p>(screenshot file could not be read)</p>"
    if len(data) > MAX_IMAGE_BYTES or len(data) > budget[0]:
        return (
            f"<p>(screenshot <code>{_e(path.name)}</code> not embedded: {len(data) // 1024} KB)</p>"
        )
    budget[0] -= len(data)
    kind = "jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "png"
    encoded = base64.b64encode(data).decode("ascii")
    return f'<img class="shot" alt="{_e(path.name)}" src="data:image/{kind};base64,{encoded}">'


def _side_html(side: ExampleSide, run_dir: Path | None, budget: list[int]) -> str:
    quality = "-" if side.quality is None else f"{side.quality:.2f}"
    out = f"<div><h4>{_e(side.arm)} (repeat {side.repeat}): completion {quality}</h4><ul>"
    for g in side.gates:
        state = {True: "✓ pass", False: "✗ FAIL", None: "? unverified"}[g.get("passed")]
        out += f"<li>gate {_e(g.get('id', ''))}: {state} {_e(g.get('detail', ''))}</li>"
    for e in side.evidence:
        out += (
            f"<li><b>{_e(e.id or e.kind)}</b> {_e(e.kind)} ({_e(e.status)}): {_e(e.summary)}</li>"
        )
    out += "</ul>"
    for name, raw in side.screenshots.items():
        found = (
            screenshot_path(run_dir, raw)
            if run_dir
            else (Path(raw) if Path(raw).is_file() else None)
        )
        out += f"<p>{_e(name)}</p>" + (
            _image(found, budget) if found else "<p>(screenshot file not found)</p>"
        )
    if side.justification:
        out += f"<p>Judge: {_e(side.justification)}</p>"
    out += f'<p class="reason">Measuring this cost {money(side.eval_cost_usd)} and {seconds(side.eval_seconds)}: not part of the arm\'s cost or time.</p>'
    return out + "</div>"


def _examples(report: Report, run_dir: Path | None) -> str:
    if not report.examples:
        return ""
    budget = [MAX_IMAGE_TOTAL]
    out = "<h2>Most different answers, with evidence</h2>"
    out += (
        "<p>The tasks with evidence where an arm and the baseline differed most in completion.</p>"
    )
    for ex in report.examples:
        out += f'<div class="card"><h3>{_e(ex.task_id)} ({_e(ex.category)}): completion differs by {ex.gap:+.2f}</h3><div class="pair">'
        out += _side_html(ex.challenger, run_dir, budget) + _side_html(ex.baseline, run_dir, budget)
        out += "</div></div>"
    return out


def _gate(report: Report) -> str:
    gate = report.judge_gate
    if gate is None:
        return ""
    shown = ", ".join(
        f"{_e(j)} {'uncalibrated' if a is None else f'{a:.0%}'}" for j, a in gate.accuracy.items()
    )
    reasons = "".join(f"<li>{_e(r)}</li>" for r in gate.reasons)
    state = "Headline verdict blocked" if gate.blocked else "Headline verdict allowed"
    return (
        f'<h2>Judge reliability</h2><div class="card"><p><b>{state}.</b> Accuracy floor {gate.floor:.0%}: {shown}.</p>'
        f"<ul>{reasons}</ul></div>"
    )


def render_html(report: Report, run_dir: Path | None = None) -> str:
    """The report as one HTML document. ``run_dir`` lets screenshots be found beside the run."""
    run = report.run
    notes = "".join(f'<div class="note">{_e(n)}</div>' for n in report.notes)
    facts = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in methodology(report))
    sim = " (simulated)" if run.mock else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Benchmark report {_e(run.run_id)}</title><style>{_CSS}</style></head><body><main>"
        f'<header><div><h1>Benchmark report{sim}</h1><p class="sub">{_e(run.run_id)} · '
        f"{_e(run.dataset)} · {len(report.stats.arms)} arms · baseline {_e(report.stats.baseline)}</p></div>"
        '<button id="theme" type="button">Light / dark</button></header>'
        f"{notes}<h2>Headline</h2>"
        "<p>Each arm against the baseline on all tasks. A verdict is yes, no or inconclusive; "
        "inconclusive means the study cannot tell.</p>"
        f"{_verdict_cards(report)}"
        "<h2>Arms</h2>"
        f"{_legend(report)}{_pareto_charts(report)}{_category_chart(report)}{_timeline(report)}"
        f'<div class="card">{_arm_table(report)}</div>'
        "<h2>Comparisons by category</h2>"
        f'<div class="card">{_comparison_table(report)}</div>'
        f'<div class="card">{_difficulty_table(report)}{_variance_table(report)}</div>'
        f"{_gate(report)}{_examples(report, run_dir)}"
        f'<h2>Methodology</h2><div class="card"><dl>{facts}</dl></div>'
        f'<div id="tip" role="tooltip"></div><script>{_JS}</script></main></body></html>'
    )


def cost_quality_svg(report: Report) -> str:
    """The cost-versus-quality chart as a stand-alone SVG file (for a README). The page's colour
    tokens and chart rules travel inside it, light and dark included, so it renders anywhere an
    image does."""
    dots = [
        Dot(
            a.arm,
            _slot(report, a.arm),
            a.cost_per_task.estimate,
            a.mean_quality.estimate,
            a.cost_per_task.low,
            a.cost_per_task.high,
            a.mean_quality.low,
            a.mean_quality.high,
            a.arm in report.stats.pareto_cost,
            _arm_tip(a),
        )
        for a in report.stats.arms
        if a.mean_quality is not None and a.cost_per_task is not None
    ]
    chart = scatter_chart(
        dots,
        x_label="Cost per task (USD); bars are 95% intervals",
        y_label="Mean quality",
        x_format="${:g}",
        title="Quality against cost per task",
        desc="One point per arm. Up and to the left is better; the line joins the arms no other arm beats.",
    )
    tokens = _CSS[: _CSS.index("*{box-sizing")]
    rules = _CSS[_CSS.index("svg.chart{") : _CSS.index("#tip{")]
    style = f"<style>{tokens}{rules}svg.chart{{background:var(--surface)}}</style>"
    opening_end = chart.index(">") + 1
    namespaced = chart.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
    opening_end += len(' xmlns="http://www.w3.org/2000/svg"')
    return f"{namespaced[:opening_end]}{style}{namespaced[opening_end:]}\n"
