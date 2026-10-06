"""``visual`` and ``console``: look at the page the answer built, as a person would.

``BrowserDriver`` opens the answer's static site in headless Chromium (Playwright; the optional
extra ``fusion-code-orchestrator[bench-visual]`` and ``playwright install chromium``) at a phone
(390x844) and a desktop (1440x900) viewport, and reports screenshots (full page and above the
fold), console errors, failed requests, horizontal overflow, the keyboard focus probe and the
accessibility rules (``a11y``). One capture serves ``visual``, ``console`` and ``a11y``, so the
page is opened once per answer.

**The browser is the one thing that does not run inside ``Sandbox``.** Chromium has a sandbox of
its own that cannot nest in ``sandbox-exec``, so the page is contained differently: it is served by
a throwaway ``http.server`` bound to 127.0.0.1 over a *copy* of the answer's files; Chromium is
started with every host but 127.0.0.1 unresolvable and each request that is not for that server,
a ``data:`` URL or a ``blob:`` URL is aborted and listed as a failed request; the profile is a
temporary directory, service workers and downloads are off, and the whole capture has a time limit.
Without Playwright (or a browser) the evaluators report ``skipped``, not a failure, and the
screenshots-dependent gates and criteria are left out rather than guessed. See SECURITY.md.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import importlib.util
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fusion.bench.evaluators.base import Evidence, read_tree, tree_digest
from fusion.bench.spec import BenchTask
from fusion.bench.virtual import offload

__all__ = [
    "AXE_FILE",
    "BROWSER_PATH_ENV",
    "INSTALL_HINT",
    "VIEWPORTS",
    "BrowserDriver",
    "BrowserReport",
    "ConsoleEvaluator",
    "PlaywrightDriver",
    "ViewportReport",
    "VisualEvaluator",
    "playwright_status",
]

ASSETS = Path(__file__).parent / "assets"
AXE_FILE = ASSETS / "axe.min.js"  # drop axe-core here to use it instead of the built-in rules
VIEWPORTS: dict[str, tuple[int, int]] = {"mobile": (390, 844), "desktop": (1440, 900)}
INSTALL_HINT = "uv sync --extra bench-visual && uv run playwright install chromium"
BROWSER_PATH_ENV = "FUSION_BROWSER_PATH"  # a Chromium or Chrome to use instead of Playwright's own
_LOCAL_SCHEMES = ("data:", "blob:", "about:")


@dataclass
class ViewportReport:
    """What the page showed at one viewport."""

    name: str
    full_page: Path | None = None
    above_fold: Path | None = None
    overflow_x_px: float = 0.0
    text_chars: int = 0  # characters of visible text: 0 is a blank page
    focusable: int = 0
    focus_visible: int = 0
    focus_checked: int = 0
    violations: list[dict[str, Any]] = field(default_factory=list)  # axe-shaped
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    load_error: str = ""


@dataclass
class BrowserReport:
    viewports: dict[str, ViewportReport] = field(default_factory=dict)
    engine: str = "chromium"
    a11y_engine: str = "builtin"  # "axe-core" when AXE_FILE exists
    error: str = ""  # the browser could not be used at all

    def screenshots(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for name, vp in self.viewports.items():
            for label, path in (("full", vp.full_page), ("fold", vp.above_fold)):
                if path is not None:
                    found[f"{name}-{label}"] = path
        return found


class BrowserDriver(Protocol):
    """Opens a copy of a site and reports on it. Blocking; ``available`` says why not, if not."""

    def available(self) -> tuple[bool, str]: ...

    def capture(self, site: Path, entry: str, out_dir: Path, timeout_s: float) -> BrowserReport: ...


def playwright_status() -> tuple[bool, str]:
    """Whether Playwright is installed (the browser itself is found on first launch)."""
    if importlib.util.find_spec("playwright") is None:
        return False, f"Playwright is not installed ({INSTALL_HINT})"
    return True, "playwright"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


class PlaywrightDriver:
    """The real driver: headless Chromium through Playwright's sync API."""

    def __init__(self) -> None:
        self._status: tuple[bool, str] | None = None

    def available(self) -> tuple[bool, str]:
        if self._status is None:
            self._status = playwright_status()
        return self._status

    def capture(self, site: Path, entry: str, out_dir: Path, timeout_s: float) -> BrowserReport:
        ok, why = self.available()
        if not ok:
            return BrowserReport(error=why)
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        out_dir.mkdir(parents=True, exist_ok=True)
        handler = functools.partial(_QuietHandler, directory=str(site))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        report = BrowserReport(a11y_engine="axe-core" if AXE_FILE.is_file() else "builtin")
        try:
            with sync_playwright() as pw:
                own = os.environ.get(BROWSER_PATH_ENV, "").strip() or None
                try:
                    browser = pw.chromium.launch(
                        headless=True,
                        executable_path=own,
                        args=[
                            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                            "--disable-dev-shm-usage",
                        ],
                    )
                except PlaywrightError as exc:
                    report.error = f"the browser could not start: {str(exc).splitlines()[0]}"
                    return report
                try:
                    for name, (width, height) in VIEWPORTS.items():
                        report.viewports[name] = self._one(
                            browser, name, width, height, port, entry, out_dir, timeout_s
                        )
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
        return report

    def _one(
        self,
        browser: Any,
        name: str,
        width: int,
        height: int,
        port: int,
        entry: str,
        out_dir: Path,
        timeout_s: float,
    ) -> ViewportReport:
        report = ViewportReport(name=name)
        origin = f"http://127.0.0.1:{port}"
        context = browser.new_context(
            viewport={"width": width, "height": height},
            service_workers="block",
            accept_downloads=False,
        )
        context.set_default_timeout(timeout_s * 1000)

        def gate(route: Any) -> None:
            url = route.request.url
            if url.startswith(origin) or url.startswith(_LOCAL_SCHEMES):
                route.continue_()
            else:
                report.failed_requests.append(f"blocked {url[:100]}")
                route.abort()

        context.route("**/*", gate)
        page = context.new_page()
        page.on(
            "console",
            lambda m: report.console_errors.append(m.text[:200]) if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: report.console_errors.append(f"uncaught: {str(e)[:200]}"))
        page.on(
            "requestfailed",
            lambda r: (
                report.failed_requests.append(f"{r.url[:100]}: {r.failure}")
                if not r.url.startswith(origin)
                else report.failed_requests.append(f"failed {r.url[:100]}")
            ),
        )
        try:
            page.goto(f"{origin}/{entry.lstrip('/')}", wait_until="load")
            page.wait_for_timeout(250)
            report.overflow_x_px = float(
                page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            )
            report.text_chars = int(page.evaluate("(document.body.innerText || '').trim().length"))
            report.full_page = out_dir / f"{name}-full.png"
            report.above_fold = out_dir / f"{name}-fold.png"
            page.screenshot(path=str(report.full_page), full_page=True)
            page.screenshot(path=str(report.above_fold), full_page=False)
            probe = page.evaluate((ASSETS / "focus_probe.js").read_text(encoding="utf-8"))
            report.focusable = int(probe["focusable"])
            report.focus_visible = int(probe["visible"])
            report.focus_checked = int(probe["checked"])
            if AXE_FILE.is_file():
                page.add_script_tag(content=AXE_FILE.read_text(encoding="utf-8"))
                found = page.evaluate("axe.run().then(r => r.violations)")
                report.violations = _axe_violations(found)
            else:
                report.violations = page.evaluate((ASSETS / "a11y_rules.js").read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001 — a page that will not load is a finding
            report.load_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
        finally:
            context.close()
        return report


def _axe_violations(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": v.get("id", ""),
            "impact": v.get("impact") or "minor",
            "help": v.get("help", ""),
            "nodes": [
                {"target": str(n.get("target", ""))[:80], "html": str(n.get("html", ""))[:120]}
                for n in v.get("nodes", [])[:10]
            ],
        }
        for v in found
    ]


# -- capturing once per answer -----------------------------------------------------------------


def capture_site(
    tree: dict[str, str], task: BenchTask, out_dir: Path, driver: BrowserDriver
) -> BrowserReport:
    """Open ``tree`` as a site and report. Writes the site to a temporary directory first, so
    the browser only ever sees a copy."""
    site_spec = task.artifact_truth().site
    entry = site_spec.entry if site_spec else "index.html"
    timeout = site_spec.timeout_s if site_spec else 20.0
    if entry not in tree:
        return BrowserReport(error=f"the answer has no {entry}")
    with tempfile.TemporaryDirectory(prefix="fusion-site-") as raw:
        site = Path(raw)
        for rel, text in tree.items():
            target = site / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return driver.capture(site, entry, out_dir, timeout)


class CaptureCache:
    """One capture per (answer, output folder), shared by the evaluators that read it."""

    def __init__(self, driver: BrowserDriver, out_dir: Path | None = None) -> None:
        self.driver = driver
        self.out_dir = out_dir
        self._lock = threading.Lock()
        self._reports: dict[str, BrowserReport] = {}

    def get(self, tree: dict[str, str], task: BenchTask) -> BrowserReport:
        folder = self.out_dir or Path(tempfile.gettempdir()) / "fusion-evidence"
        key = hashlib.sha256(f"{tree_digest(tree)}:{folder}".encode()).hexdigest()[:16]
        with self._lock:
            if key in self._reports:
                return self._reports[key]
            report = capture_site(tree, task, folder / key, self.driver)
            self._reports[key] = report
            return report


def _skipped(kind: str, name: str, why: str) -> Evidence:
    return Evidence(kind=kind, name=name, status="skipped", summary=why)  # type: ignore[arg-type]


class VisualEvaluator:
    """Screenshots and layout checks. ``ok`` means: the page loads, shows text, does not scroll
    sideways on a phone and nothing is cut off."""

    name = "visual"

    def __init__(self, cache: CaptureCache) -> None:
        self.cache = cache

    async def run(self, workdir: Path, task: BenchTask) -> Evidence:
        tree = read_tree(workdir)
        ok, why = self.cache.driver.available()
        if not ok:
            return _skipped("screenshot", self.name, f"skipped: {why}")
        report = await offload(self.cache.get, tree, task)
        if report.error:
            return _skipped("screenshot", self.name, f"skipped: {report.error}")
        return self._evidence(report)

    def _evidence(self, report: BrowserReport) -> Evidence:
        mobile = report.viewports.get("mobile")
        desktop = report.viewports.get("desktop")
        loads = all(not v.load_error for v in report.viewports.values())
        text = min((v.text_chars for v in report.viewports.values()), default=0)
        overflow = mobile.overflow_x_px if mobile else 0.0
        metrics = {
            "loads": 1.0 if loads else 0.0,
            "text_chars": float(text),
            "overflow_x_mobile_px": overflow,
            "overflow_x_desktop_px": desktop.overflow_x_px if desktop else 0.0,
            "screenshots": float(len(report.screenshots())),
        }
        problems = [f"{v.name}: {v.load_error}" for v in report.viewports.values() if v.load_error]
        if overflow > 1:
            problems.append(f"scrolls sideways by {overflow:.0f}px on a phone")
        if text == 0:
            problems.append("the page shows no text")
        shots = report.screenshots()
        return Evidence(
            kind="screenshot",
            name=self.name,
            ok=not problems,
            metrics=metrics,
            artifact_path=shots.get("desktop-fold") or next(iter(shots.values()), None),
            artifacts=shots,
            summary="; ".join(problems) or "loads, shows content, fits a phone screen",
        )


class ConsoleEvaluator:
    """Errors the page logged and requests that failed or were blocked."""

    name = "console"

    def __init__(self, cache: CaptureCache) -> None:
        self.cache = cache

    async def run(self, workdir: Path, task: BenchTask) -> Evidence:
        tree = read_tree(workdir)
        ok, why = self.cache.driver.available()
        if not ok:
            return _skipped("console", self.name, f"skipped: {why}")
        report = await offload(self.cache.get, tree, task)
        if report.error:
            return _skipped("console", self.name, f"skipped: {report.error}")
        errors = sorted({e for v in report.viewports.values() for e in v.console_errors})
        failed = sorted({e for v in report.viewports.values() for e in v.failed_requests})
        metrics = {"console_errors": float(len(errors)), "failed_requests": float(len(failed))}
        shown = [*errors[:3], *failed[:3]]
        return Evidence(
            kind="console",
            name=self.name,
            ok=not errors and not failed,
            metrics=metrics,
            summary="; ".join(shown) or "no console errors, no failed requests",
        )


def default_driver() -> BrowserDriver:
    return PlaywrightDriver()


DriverFactory = Callable[[], BrowserDriver]
