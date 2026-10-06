"""The helper that frontend tasks' hidden tests use to read a built site."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fusion.bench.datasets.helpers.sitecheck import Page, contrast, parse_color

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hi there</title><link rel="stylesheet" href="style.css"><style>.inline { color: #111; background: #fff }</style></head>
<body><header><nav aria-label="Main"><a href="#a">First</a><a href="#b"><img src="x.png" alt="Second"></a></nav></header>
<main><h1>Title</h1><h3>Skipped</h3>
<form><label for="e">Email</label><input id="e" type="email" required>
<label>Name <input name="n"></label><input id="u" type="text" aria-label="User">
<select name="s"></select><textarea id="t"></textarea>
<input type="hidden" name="h"><button type="submit">Go</button><button><img src="g.png" alt="Close"></button></form>
<img src="a.png" alt="An image"><img src="b.png"></main>
<script src="app.js"></script></body></html>
"""
CSS = """/* theme */
body { color: #111; background: #fff; }
.card { color: #bbb; background: #fff; width: 300px; }
.wide { width: 900px; }
a:focus-visible { outline: 2px solid blue; }
@media (max-width: 600px) { .wide { width: 100%; } .card { padding: 4px; } }
"""


@pytest.fixture
def page(tmp_path: Path):
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    (tmp_path / "style.css").write_text(CSS, encoding="utf-8")
    (tmp_path / "app.js").write_text("document.title = 'x';\n", encoding="utf-8")
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield Page()
    finally:
        os.chdir(previous)


def test_it_finds_elements_by_tag_and_attributes(page: Page) -> None:
    assert page.title == "Hi there" and page.lang == "en"
    assert [e.text for e in page.find("a")] == ["First", ""]
    assert page.first("input", type="email").has("required")
    assert page.find("label", for_="e")[0].text == "Email"
    assert page.find("nav", **{"aria-label": "Main"})
    assert page.headings() == [(1, "Title"), (3, "Skipped")]
    assert "Email" in page.text() and page.first("table") is None


def test_it_names_controls_and_links_like_a_screen_reader_would(page: Page) -> None:
    by_id = {e.get("id") or e.get("name"): page.label_of(e) for e in page.find("input")}
    assert by_id["e"] == "Email" and by_id["n"].startswith("Name") and by_id["u"] == "User"
    assert [page.name_of(a) for a in page.find("a")] == ["First", "Second"]
    assert [page.name_of(b) for b in page.find("button")] == ["Go", "Close"]
    unlabelled = page.unlabelled_controls()
    assert {e.tag for e in unlabelled} == {"select", "textarea"} and len(unlabelled) == 2
    assert len(page.images_without_alt()) == 1


def test_it_reads_linked_and_inline_css_and_scripts(page: Page) -> None:
    selectors = [s for s, _, _ in page.rules()]
    assert ".inline" in selectors and ".card" in selectors
    assert [m.strip() for m in page.media_queries()] == ["@media (max-width: 600px)"]
    assert page.declared("width") == [(".card", "300px"), (".wide", "900px"), (".wide", "100%")]
    assert "document.title" in page.js
    assert page.has_focus_style() and page.has_viewport_meta()


def test_it_judges_responsiveness_and_contrast(page: Page) -> None:
    assert page.wide_fixed_widths() == [
        (".wide", "900px")
    ]  # the one inside the media query is fine
    assert page.is_fluid() is False
    assert page.contrast_failures() == [(".card", pytest.approx(1.92, abs=0.01))]
    assert contrast(parse_color("#000"), parse_color("white")) == pytest.approx(21.0)
    assert parse_color("rgb(1, 2, 3)") == (1, 2, 3) and parse_color("nonsense") is None
