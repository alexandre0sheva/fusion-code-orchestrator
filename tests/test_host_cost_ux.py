"""Roadmap Task 17: `fusion stats` and the dashboard show the host tokens Fusion saved and the
latest Claude Code A/B (`fusion bench e2e`) next to what Fusion spent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from _dashboard_seed import seed_runs
from fusion.bench.e2e.summary import best_value, latest_e2e
from fusion.cli.main import app
from fusion.dashboard import api
from fusion.dashboard.app import STATIC_DIR, create_app
from fusion.mcp_server.tools import FusionTools
from fusion.storage.run_store import RunStore
from fusion.telemetry.stats_format import format_stats_markdown, stats_to_dict

cli = CliRunner()
BASE = "http://127.0.0.1:8765"


@pytest.fixture(scope="module")
def e2e_bench_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A simulated Claude Code A/B, made with the real command in a throwaway Fusion home."""
    base = tmp_path_factory.mktemp("e2e-home")
    root = base / "bench"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FUSION_BENCH_DIR", str(root))
        for name in ("config", "data", "project"):
            (base / name).mkdir()
        mp.setenv("FUSION_CONFIG_DIR", str(base / "config"))
        mp.setenv("FUSION_DATA_DIR", str(base / "data"))
        mp.setenv("FUSION_PROJECT_DIR", str(base / "project"))
        mp.delenv("FUSION_DB_PATH", raising=False)
        done = cli.invoke(
            app, ["bench", "e2e", "--mock", "--split", "all", "--limit", "2", "--run-id", "e2e-sim"]
        )
        assert done.exit_code == 0, done.output
    return root


@pytest.fixture(scope="module")
def runs_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("stats") / "runs.db"
    seed_runs(path, runs=12, days=5, seed=3)
    return path


# -- the summary reader -------------------------------------------------------------------------


def test_the_latest_e2e_run_is_found_and_flagged_as_simulated(e2e_bench_dir: Path) -> None:
    summary = latest_e2e(e2e_bench_dir)
    assert summary is not None and summary.run_id == "e2e-sim" and summary.mock is True
    arms = {row.arm for row in summary.rows}
    assert {"haiku+fusion", "opus"} <= arms
    best = best_value(summary.rows)
    assert best is None or best.cost_per_solved_usd is not None


def test_no_bench_store_means_no_e2e_and_none_is_created(tmp_path: Path) -> None:
    assert latest_e2e(tmp_path / "nothing") is None
    assert not (tmp_path / "nothing").exists()
    (tmp_path / "empty").mkdir()
    assert latest_e2e(tmp_path / "empty") is None
    assert list((tmp_path / "empty").iterdir()) == []


# -- fusion stats -------------------------------------------------------------------------------


def test_the_stats_text_and_record_carry_the_ab_and_say_it_is_simulated(
    runs_db: Path, e2e_bench_dir: Path
) -> None:
    store = RunStore(db_path=str(runs_db))
    summary = latest_e2e(e2e_bench_dir)
    text = format_stats_markdown(store.get_stats(), None, e2e=summary)
    assert "### Claude Code end to end" in text and "e2e-sim" in text
    assert "simulated" in text.lower()
    assert "haiku+fusion" in text
    record = stats_to_dict(store.get_stats(), None, e2e=summary)
    assert record["e2e"]["run_id"] == "e2e-sim" and record["e2e"]["mock"] is True
    assert "e2e" not in stats_to_dict(store.get_stats(), None)  # nothing measured: no section
    assert "end to end" not in format_stats_markdown(store.get_stats(), None)


def test_the_stats_command_reads_the_bench_dir(
    runs_db: Path, e2e_bench_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_BENCH_DIR", str(e2e_bench_dir))
    result = cli.invoke(app, ["stats", "--db-path", str(runs_db), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["e2e"]["run_id"] == "e2e-sim"
    text = cli.invoke(app, ["stats", "--db-path", str(runs_db)])
    assert "Claude Code end to end" in text.output


async def test_the_stats_tool_reads_the_bench_dir_too(
    tmp_path: Path, e2e_bench_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fusion.mcp_server.schemas import FusionStatsInput

    monkeypatch.setenv("FUSION_BENCH_DIR", str(e2e_bench_dir))
    tools = FusionTools(db_path=str(tmp_path / "runs.db"))
    try:
        shown = await tools.fusion_stats(FusionStatsInput())
    finally:
        await tools.aclose()
    assert "Claude Code end to end" in shown["display_markdown"]
    assert shown["result"]["e2e"]["run_id"] == "e2e-sim"


# -- the dashboard ------------------------------------------------------------------------------


def test_the_overview_has_the_host_tokens_and_the_ab(runs_db: Path, e2e_bench_dir: Path) -> None:
    src = api.Sources.resolve(str(runs_db), e2e_bench_dir)
    data = api.overview(src)
    assert data["totals"]["host_tokens_saved"] >= 0
    assert "workspace_runs" in data["totals"]
    assert data["e2e"]["run_id"] == "e2e-sim" and data["e2e"]["mock"] is True
    assert data["e2e"]["rows"]
    served = TestClient(
        create_app(db_path=str(runs_db), bench_dir=e2e_bench_dir), base_url=BASE
    ).get("/api/overview")
    assert served.status_code == 200 and served.json()["e2e"]["run_id"] == "e2e-sim"


def test_the_overview_has_no_e2e_before_any_study(runs_db: Path, tmp_path: Path) -> None:
    data = api.overview(api.Sources.resolve(str(runs_db), tmp_path / "none"))
    assert data["e2e"] is None


def test_the_page_script_draws_both_without_inline_code() -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "Host tokens saved" in script and "Claude Code end to end" in script
    assert "host_tokens_saved" in script and "o.e2e" in script
