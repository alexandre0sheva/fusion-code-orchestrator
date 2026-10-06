"""``a11y``: accessibility violations of the page, by severity.

Two engines, both reporting axe-shaped violations (``id``, ``impact``, ``nodes``):

* **browser** (when Playwright is installed): rules run in the real page, with computed styles
  for contrast (``assets/a11y_rules.js``, or axe-core itself when ``assets/axe.min.js`` has been
  placed there: no CDN is ever used), plus a keyboard-focus smoke check (can the page's controls be
  focused, and does focus show?).
* **static** (always): the same rules read from the HTML source, and contrast from CSS rules
  that give both a colour and a background. It cannot see what a script adds, or cascades beyond a
  single rule, so it under-reports: a clean static result is weaker evidence than a clean browser
  one, and the evidence says which engine ran (``engine`` is 1 for the browser, 0 for static).

``ok`` means no critical or serious violation. Neither engine is a substitute for an audit.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from fusion.bench.evaluators.base import Evidence, read_tree
from fusion.bench.evaluators.visual import BrowserReport, CaptureCache
from fusion.bench.spec import BenchTask
from fusion.bench.virtual import offload

__all__ = ["A11yEvaluator", "contrast_ratio", "parse_color", "static_violations"]

_IMPACTS = ("critical", "serious", "moderate", "minor")
_NAMED = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "silver": (192, 192, 192),
    "lightgray": (211, 211, 211),
    "lightgrey": (211, 211, 211),
    "yellow": (255, 255, 0),
    "blue": (0, 0, 255),
    "navy": (0, 0, 128),
    "green": (0, 128, 0),
    "orange": (255, 165, 0),
    "pink": (255, 192, 203),
    "lightblue": (173, 216, 230),
}
_RULE = re.compile(r"([^{}@]+)\{([^{}]*)\}")
_DECL = re.compile(r"(?<![-\w])(color|background(?:-color)?)\s*:\s*([^;}]+)", re.IGNORECASE)
_SKIP_INPUT = {"hidden", "submit", "button", "reset", "image"}


def parse_color(text: str) -> tuple[int, int, int] | None:
    """``#rgb``, ``#rrggbb``, ``rgb(r, g, b)`` and a few names as an (r, g, b) tuple."""
    value = text.strip().lower().split("!")[0].strip()
    if value in _NAMED:
        return _NAMED[value]
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits[:3])
        if len(digits) in (6, 8) and re.fullmatch(r"[0-9a-f]+", digits):
            return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)
    match = re.fullmatch(r"rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+).*\)", value)
    if match:
        r, g, b = (min(int(x), 255) for x in match.groups())
        return r, g, b
    return None


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    """WCAG contrast ratio of two colours (1 to 21)."""
    a, b = _luminance(fg), _luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


@dataclass
class _Page(HTMLParser):
    """Collects what the static rules need from one HTML document."""

    lang: bool = False
    title: str = ""
    in_title: bool = False
    main: bool = False
    headings: list[int] = field(default_factory=list)
    images_without_alt: int = 0
    controls: list[dict[str, str]] = field(default_factory=list)
    label_for: set[str] = field(default_factory=set)
    label_depth: int = 0
    buttons: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    positive_tabindex: int = 0
    _open: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(convert_charrefs=True)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang", "").strip():
            self.lang = True
        if tag == "title":
            self.in_title = True
        if tag == "main" or a.get("role") == "main":
            self.main = True
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        if tag == "img" and "alt" not in a and a.get("role") != "presentation":
            self.images_without_alt += 1
        if tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea"):
            kind = a.get("type", "text").lower() if tag == "input" else tag
            if kind not in _SKIP_INPUT:
                named = bool(a.get("aria-label") or a.get("aria-labelledby") or a.get("title"))
                self.controls.append(
                    {
                        "id": a.get("id", ""),
                        "named": str(named),
                        "wrapped": str(self.label_depth > 0),
                    }
                )
        if tag in ("button", "a") or a.get("role") == "button":
            named = bool(a.get("aria-label") or a.get("aria-labelledby") or a.get("title"))
            if tag == "a" and "href" not in a:
                pass
            else:
                entry = {"tag": tag, "named": named, "text": ""}
                (self.links if tag == "a" else self.buttons).append(entry)
                self._open.append(entry)
        if tag == "input" and a.get("type", "").lower() in ("submit", "button", "reset"):
            self.buttons.append(
                {"tag": "input", "named": bool(a.get("value") or a.get("aria-label")), "text": ""}
            )
        if a.get("id"):
            self.ids.append(a["id"])
        if a.get("tabindex", "").lstrip("-").isdigit() and int(a["tabindex"]) > 0:
            self.positive_tabindex += 1
        if tag == "img" and a.get("alt", "").strip() and self._open:
            self._open[-1]["text"] += "image"

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "label":
            self.label_depth = max(0, self.label_depth - 1)
        if tag in ("button", "a") and self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        for entry in self._open:
            entry["text"] += data.strip()


def _violation(rule: str, impact: str, count: int) -> dict[str, Any]:
    return {
        "id": rule,
        "impact": impact,
        "help": rule,
        "nodes": [{"target": "", "html": ""}] * count,
    }


def _is_page_rule(selector: str) -> bool:
    return bool(re.search(r"(^|[\s,])(html|body|:root)\s*(,|$)", selector.strip()))


def _css_contrast_failures(tree: dict[str, str]) -> int:
    """Rules that set a text and a background colour with a contrast under 4.5:1: both in one rule,
    or one of them in a rule for the page itself (``body``) over the other's default."""
    css = "\n".join(
        [t for p, t in tree.items() if p.endswith(".css")]
        + re.findall(r"<style[^>]*>(.*?)</style>", "\n".join(tree.values()), re.S | re.I)
    )
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"@media[^{]*\{|@supports[^{]*\{", "", css)
    rules: list[tuple[str, dict[str, tuple[int, int, int] | None]]] = []
    for selector, body in _RULE.findall(css):
        decls: dict[str, tuple[int, int, int] | None] = {}
        for prop, value in _DECL.findall(body):
            key = "color" if prop.lower() == "color" else "background"
            decls[key] = parse_color(value)
        if decls:
            rules.append((selector.strip().lower(), decls))
    page_bg, page_fg = (255, 255, 255), (0, 0, 0)
    for selector, decls in rules:
        if _is_page_rule(selector):
            page_bg = decls.get("background") or page_bg
            page_fg = decls.get("color") or page_fg
    failures = 0
    for selector, decls in rules:
        fg, bg = decls.get("color"), decls.get("background")
        if fg is not None and bg is not None:
            checked = contrast_ratio(fg, bg)
        elif (fg is not None or bg is not None) and _is_page_rule(selector):
            checked = contrast_ratio(fg or page_fg, bg or page_bg)
        else:
            continue  # the other colour comes from a rule this cannot trace: do not guess
        if checked < 4.5:
            failures += 1
    return failures


def static_violations(tree: dict[str, str]) -> list[dict[str, Any]]:
    """Accessibility violations readable from the source of the answer's HTML and CSS."""
    found: list[dict[str, Any]] = []
    pages = [t for p, t in tree.items() if p.endswith((".html", ".htm"))]
    if not pages:
        return found
    for text in pages:
        page = _Page()
        page.feed(text)
        page.close()
        checks = [
            ("image-alt", "critical", page.images_without_alt),
            (
                "label",
                "critical",
                sum(
                    1
                    for c in page.controls
                    if c["named"] != "True"
                    and c["wrapped"] != "True"
                    and c["id"] not in page.label_for
                ),
            ),
            (
                "button-name",
                "critical",
                sum(1 for b in page.buttons if not (b["named"] or b["text"])),
            ),
            (
                "link-name",
                "serious",
                sum(1 for ln in page.links if not (ln["named"] or ln["text"])),
            ),
            ("html-has-lang", "serious", 0 if page.lang else 1),
            ("document-title", "serious", 0 if page.title.strip() else 1),
            ("tabindex", "serious", page.positive_tabindex),
            ("landmark-one-main", "moderate", 0 if page.main else 1),
            ("page-has-heading-one", "moderate", 0 if 1 in page.headings else 1),
            (
                "heading-order",
                "moderate",
                sum(1 for a, b in zip(page.headings, page.headings[1:], strict=False) if b > a + 1),
            ),
            ("duplicate-id", "minor", len(page.ids) - len(set(page.ids))),
        ]
        found += [_violation(rule, impact, n) for rule, impact, n in checks if n > 0]
    contrast = _css_contrast_failures(tree)
    if contrast:
        found.append(_violation("color-contrast", "serious", contrast))
    return found


def _tally(violations: list[dict[str, Any]]) -> dict[str, float]:
    counts: Counter[str] = Counter()
    for v in violations:
        counts[str(v.get("impact") or "minor")] += max(len(v.get("nodes", [])), 1)
    contrast = sum(
        max(len(v.get("nodes", [])), 1) for v in violations if v.get("id") == "color-contrast"
    )
    metrics = {impact: float(counts.get(impact, 0)) for impact in _IMPACTS}
    metrics["total"] = float(sum(counts.values()))
    metrics["contrast_failures"] = float(contrast)
    return metrics


class A11yEvaluator:
    """Violations by impact, from the browser when there is one, else from the source."""

    name = "a11y"

    def __init__(self, cache: CaptureCache) -> None:
        self.cache = cache

    async def run(self, workdir: Path, task: BenchTask) -> Evidence:
        tree = read_tree(workdir)
        if not any(p.endswith((".html", ".htm")) for p in tree):
            return Evidence(
                kind="a11y", name=self.name, status="skipped", summary="the answer has no HTML"
            )
        available, _ = self.cache.driver.available()
        report: BrowserReport | None = (
            await offload(self.cache.get, tree, task) if available else None
        )
        if report is not None and not report.error and report.viewports:
            return self._from_browser(report)
        violations = static_violations(tree)
        metrics = _tally(violations)
        metrics["engine"] = 0.0
        return Evidence(
            kind="a11y",
            name=self.name,
            ok=metrics["critical"] == 0 and metrics["serious"] == 0,
            metrics=metrics,
            summary=_summary(violations, "static source rules only"),
        )

    def _from_browser(self, report: BrowserReport) -> Evidence:
        merged: dict[str, dict[str, Any]] = {}
        for vp in report.viewports.values():  # the worst of the viewports, per rule
            for v in vp.violations:
                keep = merged.get(v["id"])
                if keep is None or len(v["nodes"]) > len(keep["nodes"]):
                    merged[v["id"]] = v
        violations = list(merged.values())
        metrics = _tally(violations)
        desktop = report.viewports.get("desktop") or next(iter(report.viewports.values()))
        metrics["engine"] = 1.0
        metrics["focusable"] = float(desktop.focusable)
        checked = max(desktop.focus_checked, 1)
        metrics["focus_visible_ratio"] = (
            desktop.focus_visible / checked if desktop.focusable else 1.0
        )
        ok = metrics["critical"] == 0 and metrics["serious"] == 0
        if desktop.focusable and metrics["focus_visible_ratio"] < 0.5:
            ok = False
        return Evidence(
            kind="a11y",
            name=self.name,
            ok=ok,
            metrics=metrics,
            summary=_summary(violations, f"{report.a11y_engine} rules in Chromium"),
        )


def _summary(violations: list[dict[str, Any]], engine: str) -> str:
    if not violations:
        return f"no violations ({engine})"
    parts = [f"{v['id']} ({v['impact']}, {max(len(v['nodes']), 1)})" for v in violations[:6]]
    return f"{', '.join(parts)} ({engine})"
