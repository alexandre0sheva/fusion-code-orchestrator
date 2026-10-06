"""The dashboard's web app: a small Starlette application, bound to this machine only.

Read-only by construction: only GET and HEAD are routed, the run database is opened read-only, and
the pages are static files plus a JSON API. Because it has no login, it defends itself in two ways
a local service needs: it answers only requests whose ``Host`` is a loopback name (a web page on
another site cannot reach it through DNS rebinding), and it sends a strict Content-Security-Policy
(nothing is loaded from, or sent to, any other origin).
"""

from __future__ import annotations

import json
import socket
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from fusion.dashboard import api

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})

# Nothing from anywhere else; inline styles and scripts are not allowed in our own pages.
PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'self'"
)
# The benchmark report is one self-contained page with its own inline style and script.
REPORT_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)


def host_name(header: str) -> str:
    """The host part of a ``Host`` header, without the port (IPv6 literals keep their brackets)."""
    value = header.strip().lower()
    if value.startswith("["):
        return value[: value.find("]") + 1] if "]" in value else value
    return value.split(":", 1)[0]


class LocalOnly:
    """Refuse a request whose Host header is not a loopback name, and add the security headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        host = host_name(headers.get(b"host", b"").decode("latin-1"))
        if host not in LOOPBACK_HOSTS:
            response = Response("Forbidden: this dashboard answers on localhost only.", 403)
            await response(scope, receive, send)
            return

        async def with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message["headers"]}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                    (b"content-security-policy", PAGE_CSP.encode()),
                ]
                # A response that set one of these itself (the report page) keeps its own.
                extra = [(k, v) for k, v in extra if k not in existing]
                message["headers"] = [*message["headers"], *extra]
            await send(message)

        await self.app(scope, receive, with_headers)  # type: ignore[arg-type]


def _json(data: Any, status: int = 200) -> Response:
    # ``allow_nan=False`` is what JSON needs; ``api.clean`` has already turned NaN into null.
    return Response(
        json.dumps(data, default=str, allow_nan=False),
        status_code=status,
        media_type="application/json",
    )


def create_app(*, db_path: str | None = None, bench_dir: Path | None = None) -> Starlette:
    """The dashboard application. Nothing is opened until a request arrives."""
    src = api.Sources.resolve(db_path, bench_dir)

    def endpoint(
        work: Callable[[Request], Any],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def handler(request: Request) -> Response:
            try:
                return _json(await run_in_threadpool(work, request))
            except api.NotFoundError as exc:
                return _json({"error": str(exc)}, 404)
            except Exception as exc:
                # Not a traceback to the browser: the cause, in one line.
                return _json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        return handler

    def number(request: Request, name: str, default: int) -> int:
        try:
            return int(request.query_params.get(name, default))
        except ValueError:
            return default

    def text(request: Request, name: str) -> str | None:
        return request.query_params.get(name) or None

    async def index(_request: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    async def bench_page(request: Request) -> Response:
        def build() -> str:
            from fusion.bench.report import render_html

            report, run_dir = api.bench_report(src, request.path_params["run_id"])
            return render_html(report, run_dir)

        try:
            html = await run_in_threadpool(build)
        except api.NotFoundError as exc:
            return HTMLResponse(f"<p>{exc}</p>", 404)
        except Exception as exc:
            return HTMLResponse(f"<p>The report could not be built: {type(exc).__name__}</p>", 500)
        # The report is shown in a sandboxed frame, whose origin is opaque: the browser would
        # refuse a same-origin-only page there. It is still only framable by this dashboard.
        return HTMLResponse(
            html,
            headers={
                "Content-Security-Policy": REPORT_CSP,
                "Cross-Origin-Resource-Policy": "cross-origin",
            },
        )

    async def bench_json(request: Request) -> Response:
        def build() -> Any:
            report, _ = api.bench_report(src, request.path_params["run_id"])
            return api.clean(report.model_dump(mode="json"))

        try:
            return _json(await run_in_threadpool(build))
        except api.NotFoundError as exc:
            return _json({"error": str(exc)}, 404)
        except Exception as exc:
            return _json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    routes = [
        Route("/", index),
        Route("/api/meta", endpoint(lambda _r: api.meta(src))),
        Route("/api/overview", endpoint(lambda r: api.overview(src, number(r, "days", 90)))),
        Route(
            "/api/runs",
            endpoint(
                lambda r: api.runs(
                    src,
                    task=text(r, "task"),
                    strategy=text(r, "strategy"),
                    status=text(r, "status"),
                    q=text(r, "q"),
                    limit=number(r, "limit", 50),
                    offset=number(r, "offset", 0),
                )
            ),
        ),
        Route(
            "/api/runs/{run_id}", endpoint(lambda r: api.run_detail(src, r.path_params["run_id"]))
        ),
        Route("/api/bench", endpoint(lambda _r: api.bench_runs(src))),
        Route(
            "/api/bench/compare",
            endpoint(
                lambda r: api.bench_compare(
                    src, r.query_params.get("before", ""), r.query_params.get("after", "")
                )
            ),
        ),
        Route("/api/bench/{run_id}", bench_json),
        Route("/bench/{run_id}/report.html", bench_page),
        Route("/api/config", endpoint(lambda _r: api.config_view(src))),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(LocalOnly)
    return app


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def serve(*, port: int, db_path: str | None = None, open_browser: bool = False) -> None:
    """Run the dashboard on 127.0.0.1 until interrupted."""
    import uvicorn

    app = create_app(db_path=db_path)
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


__all__ = ["JSONResponse", "create_app", "serve"]
