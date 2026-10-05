"""SQLite hygiene: WAL, one connection per store, migration 4, concurrency, legacy import."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from fusion.mcp_server.server import create_mcp_server
from fusion.orchestration.pipelines import PipelineContext, Settings, build_pipelines
from fusion.routing.classifier import TaskType
from fusion.storage.legacy import LegacyImportError, import_legacy_db
from fusion.storage.migrations import SCHEMA_VERSION
from fusion.storage.run_store import RunStepRecord, RunStore, ShadowComparisonRecord


def _create(store: RunStore, label: str = "x") -> str:
    return store.create_run(task_type="ask", input_data={"q": label}, sanitized_input={"q": label})


def _raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def test_database_uses_wal_mode(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    store = RunStore(db_path=str(db))
    _create(store)
    with _raw(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    store.close()


def test_migration_4_adds_indexes(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    store = RunStore(db_path=str(db))
    _create(store)
    with _raw(db) as conn:
        names = {r[1] for r in conn.execute("SELECT * FROM sqlite_master WHERE type='index'")}
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert {"idx_runs_created_at", "idx_shadow_run_id"} <= names
    assert version == SCHEMA_VERSION == 5
    store.close()


def test_a_v3_database_is_upgraded_in_place(tmp_path: Path) -> None:
    from fusion.storage import migrations

    db = tmp_path / "old.db"
    conn = _raw(db)
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    for version, sql in migrations._MIGRATIONS.items():
        if version <= 3:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
    conn.commit()
    conn.close()
    store = RunStore(db_path=str(db))
    assert store.get_stats().total_runs == 0
    store.close()
    with _raw(db) as check:
        indexes = {r[1] for r in check.execute("SELECT * FROM sqlite_master WHERE type='index'")}
    assert "idx_runs_created_at" in indexes


def test_a_store_reuses_one_connection(tmp_path: Path) -> None:
    store = RunStore(db_path=str(tmp_path / "runs.db"))
    _create(store)
    first = store._conn
    store.get_stats()
    store.list_runs()
    assert store._conn is first
    store.close()
    assert store._conn is None
    store.close()  # idempotent
    _create(store)  # reopens lazily
    store.close()


def test_threads_can_share_one_store(tmp_path: Path) -> None:
    store = RunStore(db_path=str(tmp_path / "runs.db"))
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(20):
                rid = _create(store, f"{n}-{i}")
                store.complete_run(
                    rid,
                    status="completed",
                    output_data={},
                    trace={},
                    total_cost_usd=0.01,
                    total_latency_ms=1.0,
                    steps=[RunStepRecord(step_name="panel")],
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.get_stats().completed_runs == 120
    store.close()


def test_separate_stores_can_write_the_same_file_concurrently(tmp_path: Path) -> None:
    db = str(tmp_path / "runs.db")
    errors: list[BaseException] = []

    def work(n: int) -> None:
        store = RunStore(db_path=db)
        try:
            for i in range(15):
                _create(store, f"{n}-{i}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=work, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    reader = RunStore(db_path=db)
    assert reader.get_stats().total_runs == 75
    reader.close()


async def test_async_wrappers_run_in_a_worker_thread(tmp_path: Path) -> None:
    store = RunStore(db_path=str(tmp_path / "runs.db"))
    main_thread = threading.get_ident()
    seen: list[int] = []
    original = store.create_run

    def spy(**kwargs: Any) -> str:
        seen.append(threading.get_ident())
        return original(**kwargs)

    store.create_run = spy  # type: ignore[method-assign]
    run_id = await store.acreate_run(task_type="ask", input_data={}, sanitized_input={})
    await store.acomplete_run(
        run_id,
        status="completed",
        output_data={},
        trace={},
        total_cost_usd=0.0,
        total_latency_ms=0.0,
        steps=[],
    )
    await store.arecord_shadow_comparison(
        ShadowComparisonRecord(run_id=run_id, baseline_model="b", winner="fusion")
    )
    stats = await store.aget_stats()
    assert seen and seen[0] != main_thread
    assert stats.completed_runs == 1 and stats.shadow_total == 1
    store.close()


async def test_lifetime_stats_are_computed_once_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(db_path=str(tmp_path / "runs.db"))
    pipelines = build_pipelines(Settings(db_path=None, run_store=store, use_mock=True))
    calls = 0
    original = store.get_stats

    def counting() -> Any:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(store, "get_stats", counting)
    ctx = PipelineContext(task_type=TaskType.DEFAULT, primary_content="How do I retry?")
    await pipelines["ask"].run(ctx)
    assert calls == 1
    store.close()


# ---------------------------------------------------------------------------- location hygiene


async def test_mcp_server_creates_no_files_in_the_hosts_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fusion_home: Path
) -> None:
    from fastmcp import Client

    host_cwd = tmp_path / "host-project"
    host_cwd.mkdir()
    monkeypatch.chdir(host_cwd)
    monkeypatch.delenv("FUSION_PROJECT_DIR")
    server = create_mcp_server()
    async with Client(server) as client:
        await client.call_tool("fusion_ask", {"input": {"prompt": "hello", "context": ""}})
    assert list(host_cwd.iterdir()) == []
    assert (fusion_home / "data" / "runs.db").exists()


# -------------------------------------------------------------------------------- legacy import


def _legacy_db(path: Path, runs: int = 2) -> Path:
    store = RunStore(db_path=str(path))
    for i in range(runs):
        _create(store, f"legacy-{i}")
    store.close()
    # Pretend it is an old rollback-journal database, as v0.1.0 wrote it.
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_legacy_import_copies_runs_and_leaves_the_source_untouched(tmp_path: Path) -> None:
    legacy = _legacy_db(tmp_path / "fusion_runs.db", runs=3)
    before = (_digest(legacy), legacy.stat().st_mtime_ns)
    dest = tmp_path / "data" / "runs.db"
    result = import_legacy_db(legacy, dest)
    assert result.runs == 3
    assert (_digest(legacy), legacy.stat().st_mtime_ns) == before
    store = RunStore(db_path=str(dest))
    assert store.get_stats().total_runs == 3
    store.close()


def test_legacy_import_refuses_to_merge_into_a_database_that_has_runs(tmp_path: Path) -> None:
    legacy = _legacy_db(tmp_path / "fusion_runs.db")
    dest = tmp_path / "runs.db"
    existing = RunStore(db_path=str(dest))
    _create(existing)
    existing.close()
    with pytest.raises(LegacyImportError, match="already contains"):
        import_legacy_db(legacy, dest)


def test_legacy_import_reports_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(LegacyImportError, match="not found"):
        import_legacy_db(tmp_path / "nope.db", tmp_path / "runs.db")


def test_legacy_db_in_cwd_is_never_touched_by_normal_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _legacy_db(tmp_path / "fusion_runs.db")
    before = _digest(legacy)
    monkeypatch.chdir(tmp_path)
    store = RunStore()
    _create(store)
    store.close()
    assert _digest(legacy) == before
