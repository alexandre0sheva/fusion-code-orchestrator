"""The local dashboard: its API on a seeded database, its privacy and security promises, its pages.

Nothing here needs a network or a key: the benchmark is a simulated (mock) run, the database is
seeded by ``_dashboard_seed``, and the real server test binds 127.0.0.1 only.
"""

from __future__ import annotations

import re
import socket
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from _dashboard_seed import RAW_SECRET, seed_runs
from fusion.cli.main import app as cli_app
from fusion.dashboard import api
from fusion.dashboard.app import STATIC_DIR, create_app, host_name, port_is_free

N_RUNS = 40
BASE = "http://127.0.0.1:8765"


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str]]:
    path = tmp_path_factory.mktemp("dash") / "runs.db"
    ids = seed_runs(path, runs=N_RUNS, days=14, seed=11)
    return path, ids


@pytest.fixture
def client(seeded: tuple[Path, list[str]], tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(db_path=str(seeded[0]), bench_dir=tmp_path / "bench"), base_url=BASE
    )


def get(client: TestClient, url: str, **kwargs: Any) -> Any:
    response = client.get(url, **kwargs)
    assert response.status_code == 200, (url, response.text[:300])
    return response.json()


# -- overview ---------------------------------------------------------------------------------


def test_overview_reports_spend_against_the_baseline(client: TestClient) -> None:
    data = get(client, "/api/overview")
    assert not data["empty"] and data["totals"]["runs"] == N_RUNS
    paired = data["paired"]
    assert paired["runs"] == N_RUNS and paired["baseline_usd"] > paired["fusion_usd"] > 0
    assert paired["savings_usd"] == pytest.approx(paired["baseline_usd"] - paired["fusion_usd"])
    assert 50 < paired["savings_percent"] < 100
    assert sum(d["runs"] for d in data["daily"]) == N_RUNS
    assert [d["day"] for d in data["daily"]] == sorted(d["day"] for d in data["daily"])
    assert sum(t["runs"] for t in data["by_task"]) == N_RUNS
    assert sum(s["runs"] for s in data["by_strategy"]) == N_RUNS
    assert sum(data["by_status"].values()) == N_RUNS


def test_the_shadow_win_rate_comes_with_an_honest_interval(client: TestClient) -> None:
    shadow = get(client, "/api/overview")["shadow"]
    assert shadow["total"] == shadow["fusion_wins"] + shadow["baseline_wins"] + shadow["ties"] > 0
    assert 0 <= shadow["ci_low"] <= shadow["win_rate"] <= shadow["ci_high"] <= 1
    assert len(shadow["recent"]) <= 8


def test_wilson_interval_matches_known_values() -> None:
    low, high = api.wilson(50, 100)
    assert (low, high) == pytest.approx((0.4038, 0.5962), abs=1e-3)
    assert api.wilson(0, 0) == (0.0, 1.0)
    assert api.wilson(10, 10)[1] == pytest.approx(1.0)


# -- runs ---------------------------------------------------------------------------------------


def test_runs_list_is_newest_first_and_paged(client: TestClient) -> None:
    first = get(client, "/api/runs?limit=15")
    assert first["total"] == N_RUNS and len(first["rows"]) == 15
    stamps = [r["created_at"] for r in first["rows"]]
    assert stamps == sorted(stamps, reverse=True)
    second = get(client, "/api/runs?limit=15&offset=15")
    assert not {r["run_id"] for r in first["rows"]} & {r["run_id"] for r in second["rows"]}
    assert len(get(client, "/api/runs?limit=100000")["rows"]) == N_RUNS  # clamped, not refused
    row = first["rows"][0]
    assert {"run_id", "task_type", "status", "strategy", "cost_usd", "latency_ms"} <= set(row)


def test_runs_filter_by_task_strategy_status_and_text(client: TestClient) -> None:
    everything = get(client, "/api/runs?limit=200")
    facets = everything["facets"]
    assert "panel-duo" in facets["strategy"] and "completed" in facets["status"]
    for key, param in (("task_type", "task"), ("strategy", "strategy"), ("status", "status")):
        value = everything["rows"][0][key]
        found = get(client, f"/api/runs?{param}={value}&limit=200")
        assert found["total"] == sum(1 for r in everything["rows"] if r[key] == value)
        assert all(r[key] == value for r in found["rows"])
    hits = get(client, "/api/runs?q=sliding%20window&limit=200")
    assert 0 < hits["total"] < N_RUNS
    assert get(client, "/api/runs?q=%25")["total"] == N_RUNS  # a wildcard is not a wildcard


def test_filtering_cannot_inject_sql(client: TestClient) -> None:
    for value in ("x' OR '1'='1", "'; DROP TABLE runs;--", "%27%20OR%201=1"):
        assert get(client, "/api/runs", params={"task": value, "q": value})["total"] == 0
    assert get(client, "/api/overview")["totals"]["runs"] == N_RUNS


def test_run_detail_has_the_answer_claims_and_timeline(
    client: TestClient, seeded: tuple[Path, list[str]]
) -> None:
    run = get(client, f"/api/runs/{seeded[1][0]}")
    assert run["answer"]["text"].startswith("## Fusion Answer")
    groups = {c["group"] for c in run["claims"]}
    assert groups <= {"consensus", "unique", "contradicted", "outliers"} and "consensus" in groups
    timeline = run["timeline"]
    assert timeline["calls"] and timeline["total_ms"] == max(c["end_ms"] for c in timeline["calls"])
    starts = [c["start_ms"] for c in timeline["calls"]]
    assert starts == sorted(starts)
    first = timeline["calls"][0]
    assert first["stage"] == "panel" and first["tokens_per_s"] and first["cost_usd"] is not None
    assert {s["stage"] for s in run["stages"]} >= {"panel"}
    assert run["cost_comparison"]["baseline_estimated_cost_usd"] > run["cost_usd"]
    assert run["routing"]["strategy"] == run["strategy"]


def test_unknown_or_hostile_run_ids_are_404(client: TestClient) -> None:
    for run_id in ("run_nope", "..", "...", "a%2Fb", "x" * 200, "run_1;drop"):
        assert client.get(f"/api/runs/{run_id}").status_code == 404, run_id


# -- privacy ------------------------------------------------------------------------------------


def _every_endpoint(client: TestClient, ids: list[str]) -> str:
    urls = ["/api/meta", "/api/overview", "/api/runs?limit=200", "/api/config", "/api/bench"]
    urls += [f"/api/runs/{run_id}" for run_id in ids]
    return "\n".join(client.get(url).text for url in urls)


def test_the_unsanitized_prompt_is_never_served_by_default(
    client: TestClient, seeded: tuple[Path, list[str]]
) -> None:
    text = _every_endpoint(client, seeded[1])
    assert RAW_SECRET not in text and "DO-NOT-LEAK" not in text
    run = get(client, f"/api/runs/{seeded[1][0]}")
    assert run["prompt"]["raw"] is False and "[REDACTED]" in run["prompt"]["primary"]["text"]
    assert any(get(client, f"/api/runs/{i}")["prompt"]["redaction_count"] for i in seeded[1])
    # And it is stored, so the guarantee is the dashboard's, not an empty column's.
    with closing(sqlite3.connect(seeded[0])) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM runs WHERE input_json LIKE ?", (f"%{RAW_SECRET}%",)
            ).fetchone()[0]
            == N_RUNS
        )


def test_the_raw_prompt_appears_only_when_the_owner_turns_logging_on(
    seeded: tuple[Path, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_LOG_RAW_PROMPTS", "true")
    client = TestClient(create_app(db_path=str(seeded[0]), bench_dir=tmp_path), base_url=BASE)
    run = get(client, f"/api/runs/{seeded[1][0]}")
    assert run["prompt"]["raw"] is True and RAW_SECRET in run["prompt"]["primary"]["text"]
    assert get(client, "/api/meta")["raw_prompts"] is True
    assert get(client, "/api/config")["raw_prompts"] is True


def test_the_search_box_cannot_be_used_to_probe_the_raw_prompt(client: TestClient) -> None:
    assert get(client, "/api/runs", params={"q": "DO-NOT-LEAK"})["total"] == 0


def test_config_shows_whether_a_key_is_set_never_the_key(
    seeded: tuple[Path, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-THISMUSTNOTAPPEAR-123456")
    client = TestClient(create_app(db_path=str(seeded[0]), bench_dir=tmp_path), base_url=BASE)
    response = client.get("/api/config")
    assert "THISMUSTNOTAPPEAR" not in response.text
    assert response.json()["keys"]["anthropic"] is True


# -- read-only ---------------------------------------------------------------------------------


def test_the_database_is_opened_read_only(
    seeded: tuple[Path, list[str]], client: TestClient
) -> None:
    before = (seeded[0].stat().st_mtime_ns, seeded[0].stat().st_size)
    _every_endpoint(client, seeded[1])
    assert (seeded[0].stat().st_mtime_ns, seeded[0].stat().st_size) == before
    store = api.ReadOnlyStore(seeded[0])
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store.query("DELETE FROM runs")
    store.close()


def test_only_get_and_head_are_routed(client: TestClient) -> None:
    for method in ("post", "put", "delete", "patch"):
        for url in ("/", "/api/runs", "/api/overview", "/static/app.js"):
            assert getattr(client, method)(url).status_code in {404, 405}, (method, url)
    assert client.head("/api/meta").status_code == 200


# -- empty states ------------------------------------------------------------------------------


def test_no_database_is_an_empty_dashboard_and_creates_nothing(tmp_path: Path) -> None:
    db = tmp_path / "nothing" / "runs.db"
    client = TestClient(create_app(db_path=str(db), bench_dir=tmp_path / "bench"), base_url=BASE)
    assert get(client, "/api/overview") == {"empty": True}
    assert get(client, "/api/runs")["rows"] == [] and get(client, "/api/bench") == {"runs": []}
    assert get(client, "/api/meta")["has_db"] is False
    assert client.get("/api/runs/run_x").status_code == 404
    assert not db.parent.exists() and not (tmp_path / "bench").exists()
    assert get(client, "/api/config")["strategies"]  # configuration needs no data


def test_a_database_with_no_runs_is_empty_too(tmp_path: Path) -> None:
    from fusion.storage.run_store import RunStore

    db = tmp_path / "runs.db"
    RunStore(db_path=str(db)).close()
    client = TestClient(create_app(db_path=str(db), bench_dir=tmp_path), base_url=BASE)
    assert get(client, "/api/overview") == {"empty": True}
    assert get(client, "/api/runs")["total"] == 0


# -- the pages and the static server ------------------------------------------------------------


def test_the_page_is_self_contained_with_no_inline_code_and_no_other_origin() -> None:
    html = (STATIC_DIR / "index.html").read_text()
    assert "<title>" in html and 'lang="en"' in html and 'name="viewport"' in html
    assert 'id="view"' in html and "skip" in html.lower()
    assert not re.search(r"<style|<script(?![^>]*\bsrc=)|\sstyle=|\son\w+=", html)
    for name in ("index.html", "app.js", "style.css", "theme.js"):
        text = (STATIC_DIR / name).read_text()
        urls = re.findall(r"https?://[^\s'\")]+", text)
        assert all(u.startswith("http://www.w3.org/2000/svg") for u in urls), (name, urls)
        assert "@import" not in text, name
        # Nothing read from the database may become markup or code.
        assert not re.search(
            r"\.innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function", text
        ), name


def test_the_page_and_its_assets_are_served_with_a_strict_policy(client: TestClient) -> None:
    for url, kind in (
        ("/", "text/html"),
        ("/static/app.js", "javascript"),
        ("/static/style.css", "text/css"),
    ):
        response = client.get(url)
        assert response.status_code == 200 and kind in response.headers["content-type"]
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "'unsafe-inline'" not in csp and "http" not in csp
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert "<title>Fusion Ledger</title>" in client.get("/").text


def test_the_static_server_refuses_to_leave_its_folder(client: TestClient) -> None:
    for url in (
        "/static/../app.py",
        "/static/%2e%2e/app.py",
        "/static/..%2fapp.py",
        "/static/%2e%2e%2f%2e%2e%2fapi.py",
        "/static/....//app.py",
        "/static//etc/passwd",
        "/static/",
        "/static",
        "/static/../../../etc/hosts",
        "/static/%00app.js",
    ):
        response = client.get(url)
        assert response.status_code in {404, 307, 400}, (url, response.status_code)
        assert "create_app" not in response.text and "root:" not in response.text
    assert client.get("/static/app.js").status_code == 200


def test_only_loopback_host_names_are_answered(
    seeded: tuple[Path, list[str]], tmp_path: Path
) -> None:
    app = create_app(db_path=str(seeded[0]), bench_dir=tmp_path)
    for host, expected in (
        ("127.0.0.1:8765", 200),
        ("localhost:8765", 200),
        ("localhost", 200),
        ("[::1]:8765", 200),
        ("evil.example", 403),
        ("evil.example:8765", 403),
        ("127.0.0.1.evil.example", 403),
        ("192.168.1.5:8765", 403),
        ("0.0.0.0:8765", 403),  # noqa: S104
    ):
        response = TestClient(app, base_url="http://127.0.0.1:8765").get(
            "/api/meta", headers={"host": host}
        )
        assert response.status_code == expected, host
    assert host_name("[::1]:80") == "[::1]" and host_name("LocalHost:1") == "localhost"
    assert host_name("") == ""


def test_the_api_sends_json_without_nan(client: TestClient) -> None:
    text = client.get("/api/overview").text
    assert "NaN" not in text and "Infinity" not in text
    assert api.clean({"a": float("nan"), "b": [float("inf"), 1.5]}) == {"a": None, "b": [None, 1.5]}


# -- benchmarks (a real simulated run) ---------------------------------------------------------


@pytest.fixture(scope="module")
def bench_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two simulated benchmark runs, made with the real command in a throwaway Fusion home."""
    base = tmp_path_factory.mktemp("bench-home")
    root = base / "bench"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FUSION_BENCH_DIR", str(root))
        for name in ("config", "data", "project"):
            (base / name).mkdir()
        mp.setenv("FUSION_CONFIG_DIR", str(base / "config"))
        mp.setenv("FUSION_DATA_DIR", str(base / "data"))
        mp.setenv("FUSION_PROJECT_DIR", str(base / "project"))
        mp.delenv("FUSION_DB_PATH", raising=False)
        for run_id in ("sim-a", "sim-b"):
            result = CliRunner().invoke(
                cli_app,
                [
                    "bench",
                    "run",
                    "-d",
                    "v1",
                    "--arms",
                    "solo-cheap,panel-duo",
                    "--repeats",
                    "1",
                    "--mock",
                    "--limit",
                    "8",
                    "--run-id",
                    run_id,
                ],
            )
            assert result.exit_code == 0, result.output
    return root


@pytest.fixture
def bench_client(seeded: tuple[Path, list[str]], bench_dir: Path) -> TestClient:
    return TestClient(create_app(db_path=str(seeded[0]), bench_dir=bench_dir), base_url=BASE)


def test_benchmark_runs_are_listed(bench_client: TestClient) -> None:
    runs = get(bench_client, "/api/bench")["runs"]
    assert {r["run_id"] for r in runs} == {"sim-a", "sim-b"}
    one = next(r for r in runs if r["run_id"] == "sim-a")
    assert (
        one["mock"] is True
        and one["status"] == "completed"
        and one["done_jobs"] == one["total_jobs"]
    )
    assert one["arms"] == ["solo-cheap", "panel-duo"]


def test_the_benchmark_report_is_embedded_as_a_self_contained_page(
    bench_client: TestClient,
) -> None:
    response = bench_client.get("/bench/sim-a/report.html")
    assert response.status_code == 200 and response.text.lstrip().lower().startswith(
        "<!doctype html>"
    )
    assert "simulated" in response.text.lower()
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'self'" in csp
    assert "connect-src" not in csp and "script-src 'unsafe-inline'" in csp  # its own script only
    data = get(bench_client, "/api/bench/sim-a")
    assert data["run"]["run_id"] == "sim-a" and data["stats"]["arms"]


def test_two_benchmark_runs_can_be_compared(bench_client: TestClient) -> None:
    result = get(bench_client, "/api/bench/compare?before=sim-a&after=sim-b")
    assert result["before"] == "sim-a" and result["shared_tasks"] > 0
    assert {c["arm"] for c in result["changes"]} == {"solo-cheap", "panel-duo"}
    change = result["changes"][0]
    assert "mean_quality" in change["before"] and change["comparison"]["verdicts"]


def test_unknown_or_hostile_benchmark_ids_are_404(bench_client: TestClient) -> None:
    for url in (
        "/api/bench/nope",
        "/api/bench/..",
        "/api/bench/%2e%2e",
        "/bench/nope/report.html",
        "/bench/..%2f..%2fetc/report.html",
        "/api/bench/compare?before=sim-a&after=nope",
        "/api/bench/compare?before=..&after=sim-a",
        "/api/bench/compare",
    ):
        assert bench_client.get(url).status_code == 404, url


# -- configuration -------------------------------------------------------------------------------


def test_config_lists_strategies_models_and_staleness(client: TestClient) -> None:
    config = get(client, "/api/config")
    names = {s["name"] for s in config["strategies"]}
    assert {"panel-duo", "solo-cheap"} <= names and config["default_strategy"] in names
    assert all("mock" not in m["provider"] for m in config["models"])
    assert all(m["alias"] and "input_per_1m" in m for m in config["models"])
    assert isinstance(config["warnings"], list) and config["layers"][0]["name"]
    flagged = [m for m in config["models"] if m["warnings"]]
    assert all(w in config["warnings"] for m in flagged for w in m["warnings"])


# -- the real server and the command ------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_real_server_answers_on_loopback_only(
    seeded: tuple[Path, list[str]], tmp_path: Path
) -> None:
    import uvicorn

    port = _free_port()
    config = uvicorn.Config(
        create_app(db_path=str(seeded[0]), bench_dir=tmp_path),
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        ok = httpx.get(f"http://127.0.0.1:{port}/api/overview", timeout=10)
        assert ok.status_code == 200 and ok.json()["totals"]["runs"] == N_RUNS
        rebound = httpx.get(
            f"http://127.0.0.1:{port}/api/overview", headers={"host": "evil.example"}, timeout=10
        )
        assert rebound.status_code == 403
        assert all(
            sock.getsockname()[0] == "127.0.0.1" for s in server.servers for sock in s.sockets
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_the_command_checks_the_port_and_prints_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: list[dict[str, Any]] = []
    monkeypatch.setattr("fusion.dashboard.app.serve", lambda **kw: served.append(kw))
    ok = CliRunner().invoke(cli_app, ["dashboard", "--port", "18765", "--db-path", "x.db"])
    assert ok.exit_code == 0 and "http://127.0.0.1:18765/" in ok.stdout
    assert served == [{"port": 18765, "db_path": "x.db", "open_browser": False}]
    assert CliRunner().invoke(cli_app, ["dashboard", "--port", "0"]).exit_code == 2
    assert CliRunner().invoke(cli_app, ["dashboard", "--port", "70000"]).exit_code == 2
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        taken = busy.getsockname()[1]
        assert not port_is_free(taken)
        result = CliRunner().invoke(cli_app, ["dashboard", "--port", str(taken)])
        assert (
            result.exit_code == 1
            and "already in use" in result.stderr
            and str(taken + 1) in result.stderr
        )
    assert len(served) == 1


def test_the_api_module_has_no_write_statements() -> None:
    source = Path(api.__file__).read_text()
    writes = r"\b(INSERT INTO|UPDATE \w+ SET|DELETE FROM|DROP TABLE|ALTER TABLE|CREATE TABLE)\b"
    assert not re.search(writes, source, re.IGNORECASE)
    assert "mode=ro" in source
    # The unsanitized column is never selected; the raw input is reached only through the policy.
    assert not re.search(r"(?<!sanitized_)input_json", source)
