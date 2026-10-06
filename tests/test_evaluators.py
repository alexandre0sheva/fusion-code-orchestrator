"""Evaluators: each one on fixture projects (passing and failing tests, lint errors, a slow
function, a page), offline. The browser part runs against a fake driver, and against Chromium
only where Playwright is installed."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from _agentic import write_output
from _coding import ABSOLUTE, BUGGY, FILES, FIXED, SQUARED, coding_task
from fusion.bench.evaluators import DEFAULT_EVALUATORS, EvaluatorSet
from fusion.bench.evaluators._perf_harness import measure
from fusion.bench.evaluators.a11y import (
    A11yEvaluator,
    contrast_ratio,
    parse_color,
    static_violations,
)
from fusion.bench.evaluators.base import Memo, read_tree, tree_digest
from fusion.bench.evaluators.perf import (
    PerfEvaluator,
    SizeSample,
    mad,
    p90,
    scaling_exponent,
    summarize,
)
from fusion.bench.evaluators.static import (
    DiffStatsEvaluator,
    StaticEvaluator,
    complexity,
    new_dependencies,
    web_hints,
)
from fusion.bench.evaluators.tests import BuildEvaluator, TestsEvaluator
from fusion.bench.evaluators.visual import (
    BrowserReport,
    CaptureCache,
    ConsoleEvaluator,
    PlaywrightDriver,
    ViewportReport,
    VisualEvaluator,
)
from fusion.bench.patch import diff_files
from fusion.bench.spec import BenchTask


def tree(source: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {**FILES, "calc.py": source, **(extra or {})}


def put(root: Path, files: dict[str, str]) -> Path:
    return write_output(root, files)


def digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            h.update(path.relative_to(root).as_posix().encode() + path.read_bytes())
    return h.hexdigest()


# -- tests and build --------------------------------------------------------------------------


async def test_the_tests_evaluator_passes_a_correct_answer(tmp_path: Path) -> None:
    found = await TestsEvaluator().run(put(tmp_path / "ok", tree(FIXED)), coding_task())
    assert found.kind == "tests" and found.ok is True
    assert found.metrics["pass_fraction"] == 1.0 and found.metrics["expected"] == 3
    assert found.summary == "all 3 hidden tests pass"


async def test_the_tests_evaluator_names_what_fails(tmp_path: Path) -> None:
    found = await TestsEvaluator().run(put(tmp_path / "bad", tree(ABSOLUTE)), coding_task())
    assert found.ok is False
    assert found.metrics["passed"] == 2 and found.metrics["pass_fraction"] == pytest.approx(2 / 3)
    assert "HiddenTests.test_negative" in found.summary


async def test_the_tests_evaluator_scores_an_answer_that_deletes_a_file(tmp_path: Path) -> None:
    found = await TestsEvaluator().run(
        put(tmp_path / "gone", {"tests/test_visible.py": FILES["tests/test_visible.py"]}),
        coding_task(),
    )
    assert found.ok is False  # calc.py is gone, so nothing passes


async def test_a_task_without_hidden_tests_is_skipped_not_failed(tmp_path: Path) -> None:
    plain = BenchTask(id="p", category="frontend", prompt="Build.", truth={})
    found = await TestsEvaluator().run(put(tmp_path / "x", {"index.html": "<p>"}), plain)
    assert found.status == "skipped" and found.ok is None


async def test_build_catches_a_syntax_error_and_ignores_projects_without_code(
    tmp_path: Path,
) -> None:
    broken = await BuildEvaluator().run(
        put(tmp_path / "b", {"calc.py": "def add(a, b:\n    return a +\n"}), coding_task()
    )
    assert broken.ok is False and "calc.py" in broken.summary
    clean = await BuildEvaluator().run(put(tmp_path / "c", tree(FIXED)), coding_task())
    assert clean.ok is True and clean.metrics["python_files"] == 2
    page = await BuildEvaluator().run(put(tmp_path / "p", {"index.html": "<p>"}), coding_task())
    assert page.status == "skipped"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_build_checks_javascript_when_node_is_present(tmp_path: Path) -> None:
    bad = await BuildEvaluator().run(put(tmp_path / "j", {"app.js": "const = ;"}), coding_task())
    assert bad.ok is False and "app.js" in bad.summary
    good = await BuildEvaluator().run(
        put(tmp_path / "k", {"app.js": "const x = 1;\n"}), coding_task()
    )
    assert good.ok is True and good.metrics["js_files_checked"] == 1


# -- static -----------------------------------------------------------------------------------


def test_complexity_counts_branches_boolean_operators_and_comprehensions() -> None:
    source = (
        "def simple():\n    return 1\n\n"
        "def busy(a, b):\n"
        "    if a and b:\n        return [x for x in a if x]\n"
        "    for i in b:\n        while i:\n            i -= 1\n"
        "    return 0\n\n"
        "def outer():\n    def inner(x):\n        return x if x else 0\n    return inner\n"
    )
    scores = sorted(complexity(source))
    assert scores == [1, 1, 2, 7]  # simple, outer, inner (its own count), busy
    assert complexity("def broken(:\n") == []


def test_new_dependencies_are_third_party_imports_the_task_did_not_have() -> None:
    before = {"app.py": "import os\n"}
    after = {
        "app.py": "import os\nimport json\nimport requests\nfrom numpy import array\n",
        "util.py": "",
    }
    assert new_dependencies(before, after) == ["numpy", "requests"]
    assert new_dependencies(
        before, {"app.py": "import os\n", "package.json": '{"dependencies": {"left-pad": "1"}}'}
    ) == ["left-pad"]


def test_web_hints_read_what_a_responsive_offline_page_leaves_in_its_source() -> None:
    page = {
        "index.html": (
            '<html><head><meta name="viewport" content="width=device-width">'
            '<link href="https://cdn.example/x.css" rel="stylesheet"></head></html>'
        ),
        "style.css": ".a{width:900px} @media (max-width:600px){.a{width:100%}}",
    }
    hints = web_hints(page)
    assert hints["has_viewport_meta"] == 1.0 and hints["media_queries"] == 1.0
    assert hints["max_fixed_width_px"] == 900.0 and hints["external_requests"] == 1.0
    assert web_hints({"a.py": "x = 1"}) == {}


async def test_static_reports_lint_complexity_dependencies_and_secrets(tmp_path: Path) -> None:
    pytest.importorskip(
        "ruff", reason="ruff is a dev dependency"
    )  # the module, for `python -m ruff`
    files = tree(FIXED, {"extra.py": "import os\nimport requests\nKEY = 'AKIA1234567890ABCDEF'\n"})
    found = await StaticEvaluator().run(put(tmp_path / "s", files), coding_task())
    assert found.kind == "static" and found.ok is False
    assert found.metrics["ruff_issues"] >= 1  # the unused imports
    assert found.metrics["secrets"] == 1 and found.metrics["new_dependencies"] == 1
    assert "requests" in found.summary and "secret" in found.summary


async def test_static_flags_the_other_credential_shapes_redaction_knows(tmp_path: Path) -> None:
    stripe = "sk_" + "live_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4"
    slack = "xoxb-" + "123456789012-1234567890123-Ab3dE6gH9jK2mN5pQ8sT1vW4"
    google = "AIza" + "SyA-Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0e"
    files = tree(FIXED, {"cfg.py": f"A = '{stripe}'\nB = '{slack}'\nC = '{google}'\n"})
    found = await StaticEvaluator().run(put(tmp_path / "s", files), coding_task())
    assert found.metrics["secrets"] == 3


async def test_static_is_clean_on_clean_code_and_does_not_mistake_code_for_secrets(
    tmp_path: Path,
) -> None:
    files = tree(FIXED, {"app.js": 'const password = document.getElementById("password");\n'})
    found = await StaticEvaluator().run(put(tmp_path / "s", files), coding_task())
    assert found.metrics["secrets"] == 0 and found.metrics["max_complexity"] == 1


async def test_diff_stats_count_lines_and_files_against_the_task(tmp_path: Path) -> None:
    files = tree(FIXED, {"new.py": "a = 1\nb = 2\n"})
    del files["tests/test_visible.py"]
    found = await DiffStatsEvaluator().run(put(tmp_path / "d", files), coding_task())
    m = found.metrics
    assert (m["files_changed"], m["files_created"], m["files_deleted"]) == (3, 1, 1)
    assert m["lines_added"] == 3 and m["lines_removed"] == 1 + len(
        FILES["tests/test_visible.py"].splitlines()
    )


# -- perf -------------------------------------------------------------------------------------

WORKLOAD = (
    "from unique import unique\n\n\ndef setup(size):\n    return list(range(size)) * 2\n\n\n"
    "def run(state):\n    unique(state)\n"
)
SLOW = (
    "def unique(xs):\n    out = []\n    for x in xs:\n        if x not in out:\n"
    "            out.append(x)\n    return out\n"
)
FAST = "def unique(xs):\n    return list(dict.fromkeys(xs))\n"


def perf_task(**perf: Any) -> BenchTask:
    spec = {"sizes": [200, 400, 800], "samples": 5, "warmup": 1, "max_ratio": 2.0, **perf}
    return BenchTask.model_validate(
        {
            "id": "perf-unique",
            "category": "performance",
            "prompt": "Make unique fast.",
            "files": {"unique.py": SLOW},
            "truth": {
                "expected_pass": ["tests.test_hidden.HiddenTests.test_x"],
                "hidden_files": {"tests/bench_workload.py": WORKLOAD},
                "solution": diff_files({"unique.py": SLOW}, {"unique.py": FAST}),
                "perf": spec,
            },
        }
    )


def fake_sampler(times: dict[str, list[float]], rss: float = 20.0):
    """Medians by implementation: the sampler looks at the code to say how long it 'took'."""

    def sample(files: dict[str, str], spec: Any, size: int) -> SizeSample:
        kind = "fast" if "fromkeys" in files["unique.py"] else "slow"
        base = times[kind]
        scale = (size / spec.sizes[0]) ** (1.0 if kind == "fast" else 2.0)
        return SizeSample(size, [b * scale for b in base], rss)

    return sample


def test_the_statistics_are_the_textbook_ones() -> None:
    values = [10.0, 11.0, 12.0, 13.0, 40.0]
    assert mad(values) == 1.0  # median 12, deviations 2,1,0,1,28
    assert p90(values) == 40.0 and p90([1.0, 2.0]) == 2.0
    stats = summarize(values)
    assert (stats.median, stats.n) == (12.0, 5) and stats.noise == pytest.approx(1 / 12)
    assert scaling_exponent([100, 200, 400], [1.0, 2.0, 4.0]) == pytest.approx(1.0)
    assert scaling_exponent([100, 200, 400], [1.0, 4.0, 16.0]) == pytest.approx(2.0)
    assert scaling_exponent([100, 200], [3.0, 3.0]) == pytest.approx(0.0)


def test_the_harness_times_with_the_clock_it_is_given_and_drops_warmup() -> None:
    ticks = {"now": 0.0}
    durations = iter([9.0, 1.0, 2.0, 3.0])  # the first run is warmup

    def run(state: object) -> None:
        ticks["now"] += next(durations)

    taken = measure(
        lambda size: size, run, 10, 3, 1, clock=lambda: ticks["now"], collect=lambda: None
    )
    assert taken == [1.0, 2.0, 3.0]


async def test_a_slow_answer_is_flagged_against_the_reference(tmp_path: Path) -> None:
    times = {"fast": [0.001] * 5, "slow": [0.01] * 5}
    ev = PerfEvaluator(fake_sampler(times))
    fast = await ev.run(put(tmp_path / "f", {"unique.py": FAST}), perf_task())
    assert fast.ok is True and fast.metrics["ratio_vs_reference"] == pytest.approx(1.0)
    slow = await ev.run(put(tmp_path / "s", {"unique.py": SLOW}), perf_task())
    assert slow.ok is False and slow.metrics["ratio_vs_reference"] == pytest.approx(10.0 * 4)
    assert slow.metrics["scaling_exponent"] == pytest.approx(2.0)
    assert slow.metrics["scaling_excess"] == pytest.approx(1.0)
    assert "10" in slow.summary or "40" in slow.summary


async def test_fast_on_small_inputs_but_quadratic_fails_on_scaling_alone(tmp_path: Path) -> None:
    def sampler(files: dict[str, str], spec: Any, size: int) -> SizeSample:
        quadratic = "fromkeys" not in files["unique.py"]
        reference_time = 0.001 * size / spec.sizes[0]
        # Equal at the smallest size, but growing as n^2: the guard against tiny benchmarks.
        t = 0.001 * (size / spec.sizes[0]) ** (2 if quadratic else 1)
        return SizeSample(
            size, [t if size == spec.sizes[0] else (t if quadratic else reference_time)] * 5
        )

    ev = PerfEvaluator(sampler)
    found = await ev.run(put(tmp_path / "q", {"unique.py": SLOW}), perf_task(max_ratio=100.0))
    assert found.metrics["ratio_vs_reference"] == pytest.approx(4.0)  # within the generous ratio...
    assert (
        found.metrics["scaling_excess"] == pytest.approx(1.0) and found.ok is False
    )  # ...not within scaling


async def test_a_noisy_measurement_is_marked_unstable_and_gets_no_verdict(tmp_path: Path) -> None:
    def sampler(files: dict[str, str], spec: Any, size: int) -> SizeSample:
        return SizeSample(
            size, [0.001, 0.005, 0.0012, 0.009, 0.002]
        )  # MAD far above 35% of the median

    found = await PerfEvaluator(sampler).run(put(tmp_path / "n", {"unique.py": FAST}), perf_task())
    assert found.status == "unstable" and found.ok is None
    assert "not scored" in found.summary and found.metrics["noise"] > 0.35


async def test_a_crashing_workload_is_a_failure_with_the_reason(tmp_path: Path) -> None:
    def sampler(files: dict[str, str], spec: Any, size: int) -> SizeSample:
        return (
            SizeSample(size, error="ZeroDivisionError: division by zero")
            if size > 200
            else SizeSample(size, [0.001] * 5)
        )

    found = await PerfEvaluator(sampler).run(put(tmp_path / "c", {"unique.py": SLOW}), perf_task())
    assert found.ok is False and "ZeroDivisionError" in found.summary and found.status == "measured"


async def test_a_task_without_a_perf_block_or_workload_is_skipped(tmp_path: Path) -> None:
    ev = PerfEvaluator(fake_sampler({"fast": [0.001] * 5, "slow": [0.01] * 5}))
    plain = BenchTask(
        id="x",
        category="performance",
        prompt="p",
        truth={"expected_pass": ["a"], "hidden_files": {"a.py": ""}},
    )
    assert (await ev.run(put(tmp_path / "a", {"unique.py": FAST}), plain)).status == "skipped"
    missing = perf_task(script="tests/not_there.py")
    assert (
        "not among the task's hidden files"
        in (await ev.run(put(tmp_path / "b", {"unique.py": FAST}), missing)).summary
    )


async def test_perf_is_measured_in_the_sandbox_with_real_timing_and_memory(tmp_path: Path) -> None:
    sleeper = (
        "import time\n\n\ndef unique(xs):\n    time.sleep(len(xs) * {rate})\n    return list(xs)\n"
    )
    workload = (
        "from unique import unique\n\n\ndef setup(size):\n    return list(range(size))\n\n\n"
        "def run(state):\n    unique(state)\n"
    )
    slow_code, fast_code = sleeper.format(rate="4e-5"), sleeper.format(rate="5e-6")
    task = BenchTask.model_validate(
        {
            "id": "sleepy",
            "category": "performance",
            "prompt": "Make it fast.",
            "files": {"unique.py": slow_code},
            "truth": {
                "expected_pass": ["tests.test_hidden.HiddenTests.test_x"],
                "hidden_files": {"tests/bench_workload.py": workload},
                "solution": diff_files({"unique.py": slow_code}, {"unique.py": fast_code}),
                "perf": {
                    "sizes": [100, 200, 400],
                    "samples": 5,
                    "warmup": 1,
                    "max_ratio": 2.0,
                    "max_noise": 0.5,
                },
            },
        }
    )
    ev = PerfEvaluator()
    good = await ev.run(put(tmp_path / "g", {"unique.py": fast_code}), task)
    bad = await ev.run(put(tmp_path / "b", {"unique.py": slow_code}), task)
    assert good.status == "measured" and good.ok is True, good.summary
    assert bad.ok is False and bad.metrics["ratio_vs_reference"] > 4
    assert bad.metrics["peak_rss_mb"] > 1  # the harness reports memory too
    assert bad.metrics["median_s"] > 3 * good.metrics["median_s"]


# -- accessibility ----------------------------------------------------------------------------

GOOD_PAGE = (
    '<!doctype html><html lang="en"><head><title>Hi</title>'
    "<style>body{color:#111;background:#fff}.card{color:#333;background:#eee}</style></head>"
    '<body><main><h1>Title</h1><form><label for="e">Email</label><input id="e" type="email">'
    '<button>Send</button></form><a href="#x">Go there</a><img src="a.png" alt="A">'
    "</main></body></html>"
)
BAD_PAGE = (
    "<html><head><style>.card{color:#bbb;background:#fff}</style></head><body>"
    '<img src="x.png"><input type="text"><button></button><a href="#"></a>'
    '<h1>a</h1><h3>b</h3><div tabindex="3" id="d"></div><p id="d"></p></body></html>'
)


def test_static_rules_find_what_is_in_the_source() -> None:
    assert static_violations({"index.html": GOOD_PAGE}) == []
    found = {v["id"]: v["impact"] for v in static_violations({"index.html": BAD_PAGE})}
    assert found == {
        "image-alt": "critical",
        "label": "critical",
        "button-name": "critical",
        "link-name": "serious",
        "html-has-lang": "serious",
        "document-title": "serious",
        "tabindex": "serious",
        "landmark-one-main": "moderate",
        "heading-order": "moderate",
        "duplicate-id": "minor",
        "color-contrast": "serious",
    }
    assert static_violations({"a.py": "x = 1"}) == []


def test_colour_arithmetic_matches_wcag() -> None:
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    assert contrast_ratio((255, 255, 255), (255, 255, 255)) == pytest.approx(1.0)
    assert contrast_ratio(parse_color("#767676"), (255, 255, 255)) == pytest.approx(4.54, abs=0.02)
    assert parse_color("rgb(10, 20, 30)") == (10, 20, 30) and parse_color("#abc") == (170, 187, 204)
    assert parse_color("papayawhip") is None


def test_a_rule_whose_other_colour_is_unknown_is_not_guessed_at() -> None:
    css = {
        "index.html": (
            '<html lang="en"><head><title>x</title><style>.hero p{color:#fff}</style></head>'
            "<body><main><h1>x</h1></main></body></html>"
        )
    }
    assert static_violations(css) == []  # white text may well sit on a dark parent


class FakeDriver:
    """A browser that is not a browser: it answers with the report it was given."""

    def __init__(self, report: BrowserReport | None = None, available: bool = True) -> None:
        self.report = report or BrowserReport()
        self._available = available
        self.sites: list[Path] = []

    def available(self) -> tuple[bool, str]:
        return (True, "fake") if self._available else (False, "Playwright is not installed (hint)")

    def capture(self, site: Path, entry: str, out_dir: Path, timeout_s: float) -> BrowserReport:
        self.sites.append(site)
        assert (site / entry).is_file()
        return self.report


def viewport(name: str, **kw: Any) -> ViewportReport:
    return ViewportReport(name=name, text_chars=500, **kw)


async def test_a11y_falls_back_to_source_rules_when_there_is_no_browser(tmp_path: Path) -> None:
    cache = CaptureCache(FakeDriver(available=False))
    found = await A11yEvaluator(cache).run(
        put(tmp_path / "p", {"index.html": BAD_PAGE}), perf_task()
    )
    assert found.ok is False and found.metrics["engine"] == 0.0
    assert found.metrics["critical"] == 3 and found.metrics["contrast_failures"] == 1
    clean = await A11yEvaluator(cache).run(
        put(tmp_path / "q", {"index.html": GOOD_PAGE}), perf_task()
    )
    assert clean.ok is True and "source rules only" in clean.summary
    none = await A11yEvaluator(cache).run(put(tmp_path / "r", {"a.py": "x=1"}), perf_task())
    assert none.status == "skipped"


async def test_a11y_uses_the_browsers_findings_and_focus_probe(tmp_path: Path) -> None:
    violation = {
        "id": "color-contrast",
        "impact": "serious",
        "help": "x",
        "nodes": [{"target": "p", "html": "<p>"}] * 2,
    }
    report = BrowserReport(
        viewports={
            "mobile": viewport("mobile", violations=[violation]),
            "desktop": viewport(
                "desktop", violations=[], focusable=4, focus_visible=4, focus_checked=4
            ),
        }
    )
    found = await A11yEvaluator(CaptureCache(FakeDriver(report))).run(
        put(tmp_path / "p", {"index.html": GOOD_PAGE}), perf_task()
    )
    assert found.metrics["engine"] == 1.0 and found.metrics["serious"] == 2 and found.ok is False
    assert found.metrics["focus_visible_ratio"] == 1.0
    weak = BrowserReport(
        viewports={"desktop": viewport("desktop", focusable=4, focus_visible=1, focus_checked=4)}
    )
    unfocused = await A11yEvaluator(CaptureCache(FakeDriver(weak))).run(
        put(tmp_path / "q", {"index.html": GOOD_PAGE}), perf_task()
    )
    assert unfocused.ok is False  # no serious findings, but focus is mostly invisible


# -- visual and console -----------------------------------------------------------------------


async def test_visual_and_console_are_skipped_without_a_browser(tmp_path: Path) -> None:
    cache = CaptureCache(FakeDriver(available=False))
    root = put(tmp_path / "p", {"index.html": GOOD_PAGE})
    shot = await VisualEvaluator(cache).run(root, perf_task())
    console = await ConsoleEvaluator(cache).run(root, perf_task())
    assert shot.status == "skipped" and shot.ok is None and "not installed" in shot.summary
    assert console.status == "skipped"


async def test_visual_reports_screenshots_overflow_and_blank_pages(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    shots.mkdir()
    for name in ("mobile-full", "mobile-fold", "desktop-full", "desktop-fold"):
        (shots / f"{name}.png").write_bytes(b"\x89PNG")
    ok = BrowserReport(
        viewports={
            "mobile": viewport(
                "mobile", full_page=shots / "mobile-full.png", above_fold=shots / "mobile-fold.png"
            ),
            "desktop": viewport(
                "desktop",
                full_page=shots / "desktop-full.png",
                above_fold=shots / "desktop-fold.png",
            ),
        }
    )
    root = put(tmp_path / "p", {"index.html": GOOD_PAGE})
    found = await VisualEvaluator(CaptureCache(FakeDriver(ok))).run(root, perf_task())
    assert found.ok is True and found.metrics["screenshots"] == 4
    assert set(found.artifacts) == {"mobile-full", "mobile-fold", "desktop-full", "desktop-fold"}
    assert found.artifact_path == shots / "desktop-fold.png"
    wide = BrowserReport(
        viewports={
            "mobile": viewport("mobile", overflow_x_px=120.0),
            "desktop": viewport("desktop"),
        }
    )
    overflow = await VisualEvaluator(CaptureCache(FakeDriver(wide))).run(root, perf_task())
    assert overflow.ok is False and "sideways by 120px" in overflow.summary
    blank = BrowserReport(
        viewports={
            "mobile": ViewportReport(name="mobile"),
            "desktop": ViewportReport(name="desktop"),
        }
    )
    empty = await VisualEvaluator(CaptureCache(FakeDriver(blank))).run(root, perf_task())
    assert empty.ok is False and "no text" in empty.summary


async def test_console_collects_errors_and_failed_requests(tmp_path: Path) -> None:
    report = BrowserReport(
        viewports={
            "mobile": viewport(
                "mobile", console_errors=["boom"], failed_requests=["blocked https://cdn.x/y.js"]
            ),
            "desktop": viewport("desktop", console_errors=["boom"]),
        }
    )
    found = await ConsoleEvaluator(CaptureCache(FakeDriver(report))).run(
        put(tmp_path / "p", {"index.html": GOOD_PAGE}), perf_task()
    )
    assert found.ok is False and found.metrics == {"console_errors": 1.0, "failed_requests": 1.0}
    assert "boom" in found.summary


async def test_one_capture_serves_visual_console_and_a11y(tmp_path: Path) -> None:
    driver = FakeDriver(
        BrowserReport(viewports={"mobile": viewport("mobile"), "desktop": viewport("desktop")})
    )
    cache = CaptureCache(driver)
    root = put(tmp_path / "p", {"index.html": GOOD_PAGE})
    await VisualEvaluator(cache).run(root, perf_task())
    await ConsoleEvaluator(cache).run(root, perf_task())
    await A11yEvaluator(cache).run(root, perf_task())
    assert len(driver.sites) == 1  # the page was opened once, over a copy of the answer


async def test_a_site_without_its_entry_page_cannot_be_opened(tmp_path: Path) -> None:
    found = await VisualEvaluator(CaptureCache(FakeDriver())).run(
        put(tmp_path / "p", {"other.html": "<p>"}), perf_task()
    )
    assert found.status == "skipped" and "no index.html" in found.summary


def test_the_real_browser_driver_reports_when_playwright_is_missing() -> None:
    ok, why = PlaywrightDriver().available()
    if ok:
        pytest.skip("Playwright is installed; the real-browser test below covers it")
    assert "not installed" in why and "bench-visual" in why
    assert PlaywrightDriver().capture(Path("."), "index.html", Path("."), 5).error == why


def test_the_real_browser_takes_screenshots_and_blocks_other_hosts(tmp_path: Path) -> None:
    pytest.importorskip("playwright.sync_api", reason="the bench-visual extra is not installed")
    driver = PlaywrightDriver()
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        '<!doctype html><html lang="en"><head><title>T</title>'
        '<meta name="viewport" content="width=device-width">'
        '<script src="https://cdn.example.net/x.js"></script></head>'
        '<body><main><h1>Hello</h1><p style="width:800px">wide</p></main></body></html>',
        encoding="utf-8",
    )
    report = driver.capture(site, "index.html", tmp_path / "out", 20)
    if report.error:
        pytest.skip(f"no usable browser: {report.error}")
    mobile = report.viewports["mobile"]
    assert mobile.full_page is not None and mobile.full_page.read_bytes().startswith(b"\x89PNG")
    assert mobile.overflow_x_px > 0 and mobile.text_chars > 0
    assert any("blocked https://cdn.example.net/x.js" in r for r in mobile.failed_requests)


# -- all of them -----------------------------------------------------------------------------


async def test_evaluators_never_write_to_the_answers_directory(tmp_path: Path) -> None:
    kit = EvaluatorSet(tmp_path / "evidence", driver=FakeDriver(available=False))
    root = put(tmp_path / "answer", tree(FIXED))
    before = digest(root)
    for name in ("tests", "build", "static", "diff_stats"):
        await kit.run_one(name, root, coding_task())
    assert digest(root) == before and not list(root.rglob("__pycache__"))


async def test_an_evaluator_that_crashes_becomes_skipped_evidence(tmp_path: Path) -> None:
    class Boom:
        name = "tests"

        async def run(self, workdir: Path, task: BenchTask) -> Any:
            msg = "the disk is on fire"
            raise OSError(msg)

    kit = EvaluatorSet(driver=FakeDriver(available=False))
    kit._by_name["tests"] = Boom()  # type: ignore[assignment]
    found = await kit.run_one("tests", tmp_path, coding_task())
    assert found.kind == "tests" and found.status == "skipped"
    assert "evaluator failed: OSError: the disk is on fire" in found.summary


async def test_collect_numbers_the_evidence_for_judges_to_cite(tmp_path: Path) -> None:
    kit = EvaluatorSet(driver=FakeDriver(available=False))
    got = await kit.collect(
        put(tmp_path / "a", tree(FIXED)), coding_task(), ["tests", "diff_stats"]
    )
    assert [e.id for e in got] == ["E1", "E2"] and [e.name for e in got] == ["tests", "diff_stats"]
    assert all(e.seconds > 0 for e in got)


def test_each_category_has_its_default_evaluators() -> None:
    kit = EvaluatorSet(driver=FakeDriver(available=False))
    perf = BenchTask(id="p", category="performance", prompt="p", truth={})
    page = BenchTask(id="f", category="frontend", prompt="p", truth={})
    named = BenchTask(
        id="n", category="frontend", prompt="p", truth={"evaluators": ["static", "nope"]}
    )
    assert kit.names_for(perf) == list(DEFAULT_EVALUATORS["performance"])
    assert "perf" in kit.names_for(perf) and "visual" in kit.names_for(page)
    assert kit.names_for(named) == ["static"]  # unknown names are dropped


def test_the_memo_shares_results_but_never_keeps_a_noisy_one() -> None:
    from fusion.bench.evaluators.base import Evidence

    memo, calls = Memo(), []

    def make(status: str = "measured") -> Evidence:
        calls.append(status)
        return Evidence(kind="perf", status=status)  # type: ignore[arg-type]

    memo.get_or_run("a", make)
    memo.get_or_run("a", make)
    assert calls == ["measured"]
    memo.get_or_run("b", lambda: make("unstable"))
    memo.get_or_run("b", lambda: make("unstable"))
    assert calls == ["measured", "unstable", "unstable"]


def test_trees_are_read_as_text_and_fingerprinted(tmp_path: Path) -> None:
    root = put(
        tmp_path / "t",
        {"a.py": "x = 1\n", "d/b.txt": "hi", ".git/config": "no", "__pycache__/c.pyc": "no"},
    )
    (root / "bin.dat").write_bytes(b"\xff\xfe\x00")
    assert read_tree(root) == {"a.py": "x = 1\n", "d/b.txt": "hi"}
    assert tree_digest({"a": "1"}) != tree_digest({"a": "2"}) == tree_digest({"a": "2"})
    assert BUGGY and SQUARED  # fixtures used above through tree()
