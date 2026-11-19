"""`fusion bench e2e`: the end-to-end Claude Code A/B harness, offline, against a fake `claude`."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from _bench import write_dataset
from _coding import BUGGY, FIXED, HIDDEN, VISIBLE
from fusion.bench.e2e.claude_cli import (
    FUSION_SERVER,
    ClaudeInvocation,
    build_argv,
    claude_settings,
    is_fusion_tool,
    mcp_config,
    parse_output,
)
from fusion.bench.e2e.fake_claude import mock_recording
from fusion.bench.e2e.metrics import E2EMetrics, bench_metrics, fusion_ledger, price_host_usage
from fusion.bench.e2e.runner import (
    E2EArm,
    E2EConfig,
    E2EEnv,
    arm_matrix,
    e2e_prompt,
    e2e_tasks,
    parse_e2e_arms,
    run_e2e,
)
from fusion.bench.spec import load_dataset
from fusion.bench.spend import SpendLedger
from fusion.bench.store import BenchStore
from fusion.cli.app import app
from fusion.routing.model_registry import ModelRegistry
from fusion.storage.run_store import RunStore

cli = CliRunner()
CATALOG = ModelRegistry.for_mode(use_mock=False).models


def _haiku_cost() -> float:
    """What the recorded usage costs at the catalog's Haiku 5.5 prices (not Claude Code's $0.12)."""
    price = CATALOG["claude-haiku"].price_at()
    assert price is not None
    assert price.cached_input_per_1m is not None
    assert price.cache_write_per_1m is not None
    return (
        1_000 * price.input_per_1m
        + 900 * price.output_per_1m
        + 30_000 * price.cached_input_per_1m
        + 2_000 * price.cache_write_per_1m
    ) / 1_000_000


EXPECTED = [f"tests.test_hidden.HiddenTests.{n}" for n in ("test_add", "test_big", "test_negative")]


# -- fixtures -------------------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    """A dataset with one executable coding task (a task directory) and two review tasks."""
    root = tmp_path / "dataset"
    task = root / "coding" / "tiny-add"
    _write(
        task / "task.yaml",
        "id: tiny-add\ncategory: coding\ndifficulty: easy\ntimeout_s: 20\n"
        f"expected_pass: {json.dumps(EXPECTED)}\n",
    )
    _write(task / "prompt.md", "Fix `add` in calc.py so that it returns the sum of its arguments.")
    _write(task / "repo" / "calc.py", BUGGY)
    _write(task / "repo" / "tests" / "test_visible.py", VISIBLE)
    _write(task / "tests" / "test_hidden.py", HIDDEN)
    _write(task / "reference" / "solution" / "calc.py", FIXED)
    write_dataset(root / "review.jsonl", count=2)
    return root


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _result_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 41_000,
        "num_turns": 7,
        "result": "Fixed add in calc.py; the visible tests pass.",
        "session_id": "s-1",
        "total_cost_usd": 0.12,
        "usage": {
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 2_000,
            "cache_read_input_tokens": 30_000,
            "output_tokens": 900,
        },
        "modelUsage": {
            "claude-haiku-5-5": {
                "inputTokens": 1_000,
                "outputTokens": 900,
                "cacheReadInputTokens": 30_000,
                "cacheCreationInputTokens": 2_000,
                "costUSD": 0.12,
            }
        },
        "permission_denials": [],
    }
    event.update(overrides)
    return event


def _tool_use(name: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": f"t-{name}", "name": name, "input": {}}]
        },
    }


def _stream(fusion: bool = False, **result: Any) -> list[dict[str, Any]]:
    servers = [{"name": FUSION_SERVER, "status": "connected"}] if fusion else []
    events: list[dict[str, Any]] = [
        {"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "mcp_servers": servers},
        _tool_use("Read"),
        _tool_use("Edit"),
        _tool_use("Bash"),
    ]
    if fusion:
        events.append(_tool_use("mcp__fusion__fusion_ask"))
    events.append(_result_event(**result))
    return events


def _fake_claude(
    bin_dir: Path,
    *,
    events: list[dict[str, Any]],
    writes: dict[str, str] | None = None,
    fusion_runs: list[float] | None = None,
    log: Path,
) -> None:
    """Put an executable named ``claude`` on ``bin_dir`` that replays a recording."""
    recording = bin_dir / "recording.json"
    recording.write_text(
        json.dumps(
            {
                "events": events,
                "writes": writes or {},
                "fusion_runs": fusion_runs or [],
                "argv_log": str(log),
            }
        ),
        encoding="utf-8",
    )
    script = bin_dir / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"os.environ['FUSION_FAKE_CLAUDE'] = {str(recording)!r}\n"
        "from fusion.bench.e2e.fake_claude import main\n"
        "sys.exit(main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    return folder


def _env(tmp_path: Path, *, spend: SpendLedger | None = None, plugin: Path | None = None) -> E2EEnv:
    return E2EEnv(
        claude_cmd=["claude"],
        store=BenchStore(tmp_path / "results"),
        registry=ModelRegistry.for_mode(use_mock=False),
        spend=spend,
        plugin_dir=plugin,
        auth_env={"ANTHROPIC_API_KEY": "test-key-not-real"},
        provider_env={"OPENROUTER_API_KEY": "test-openrouter-not-real"},
    )


def _config(dataset: Path, arms: list[E2EArm], **fields: Any) -> E2EConfig:
    values: dict[str, Any] = {"dataset": dataset, "arms": arms, "max_usd": 5.0, "split": "all"}
    values.update(fields)
    return E2EConfig(**values)


# -- claude_cli: parsing what Claude Code prints --------------------------------------------------


def test_parse_single_json_result() -> None:
    parsed = parse_output(json.dumps(_result_event()))
    assert parsed.ok
    assert parsed.host_cost_usd == pytest.approx(0.12)
    assert parsed.num_turns == 7
    assert parsed.input_tokens == 33_000  # fresh + cache writes + cache reads
    assert parsed.output_tokens == 900
    assert parsed.result_text.startswith("Fixed add")


def test_parse_stream_counts_tool_calls_and_fusion_calls() -> None:
    lines = "\n".join(json.dumps(e) for e in _stream(fusion=True))
    parsed = parse_output(lines)
    assert parsed.tool_calls == {"Read": 1, "Edit": 1, "Bash": 1, "mcp__fusion__fusion_ask": 1}
    assert parsed.total_tool_calls == 4
    assert parsed.fusion_tool_calls == 1
    assert parsed.fusion_connected is True


def test_parse_falls_back_to_model_usage_cost() -> None:
    event = _result_event(total_cost_usd=None)
    event["modelUsage"] = {"a": {"costUSD": 0.25}, "b": {"costUSD": 0.5}}
    assert parse_output(json.dumps(event)).host_cost_usd == pytest.approx(0.75)


def test_parse_reports_missing_or_failed_result() -> None:
    assert not parse_output("not json at all").ok
    assert "no result" in (parse_output("").error or "")
    failed = parse_output(json.dumps(_result_event(is_error=True, subtype="error_max_turns")))
    assert not failed.ok
    assert failed.error == "error_max_turns"
    assert failed.host_cost_usd == pytest.approx(0.12)  # a failed run still cost money


def test_fusion_tool_names() -> None:
    assert is_fusion_tool("mcp__fusion__fusion_review_diff")
    assert is_fusion_tool("mcp__plugin_fusion_fusion__fusion_ask")
    assert not is_fusion_tool("mcp__other__x")
    assert not is_fusion_tool("Bash")


# -- claude_cli: what Claude Code is started with -------------------------------------------------


def test_settings_sandbox_bash_without_network_and_hide_secrets(tmp_path: Path) -> None:
    secrets = tmp_path / "aux"
    settings = claude_settings(secrets_dir=secrets)
    box = settings["sandbox"]
    assert box["enabled"] is True
    assert box["failIfUnavailable"] is True  # no silent fallback to unsandboxed commands
    assert box["allowUnsandboxedCommands"] is False
    assert box["network"]["allowedDomains"] == []
    assert box["network"]["strictAllowlist"] is True
    assert str(secrets) in box["filesystem"]["denyRead"]
    deny = settings["permissions"]["deny"]
    assert "WebFetch" in deny
    assert "WebSearch" in deny
    assert f"Read(/{secrets}/**)" in deny


def test_mcp_config_with_and_without_fusion() -> None:
    assert mcp_config(None, {}) == {"mcpServers": {}}
    config = mcp_config(["python", "-m", "fusion.cli.main", "mcp"], {"FUSION_DB_PATH": "/x"})
    server = config["mcpServers"][FUSION_SERVER]
    assert server["command"] == "python"
    assert server["args"] == ["-m", "fusion.cli.main", "mcp"]
    assert server["env"] == {"FUSION_DB_PATH": "/x"}


def test_argv_is_headless_isolated_and_capped(tmp_path: Path) -> None:
    inv = ClaudeInvocation(
        model="claude-haiku-5-5",
        prompt="Fix it",
        max_budget_usd=0.5,
        settings_path=tmp_path / "s.json",
        mcp_config_path=tmp_path / "m.json",
    )
    argv = build_argv(["claude"], inv)
    assert argv[:3] == ["claude", "-p", "Fix it"]
    joined = " ".join(argv)
    for flag in (
        "--output-format stream-json",
        "--verbose",
        "--model claude-haiku-5-5",
        "--max-budget-usd 0.5",
        "--strict-mcp-config",
        f"--mcp-config {tmp_path / 'm.json'}",
        f"--settings {tmp_path / 's.json'}",
        "--bare",
        "--no-session-persistence",
        "--permission-mode acceptEdits",
        "--permission-prompts none",
    ):
        assert flag in joined
    assert "--plugin-dir" not in argv
    assert "--append-system-prompt" not in argv
    with_plugin = build_argv(
        ["claude"],
        ClaudeInvocation(
            model="m",
            prompt="p",
            max_budget_usd=1.0,
            settings_path=tmp_path / "s.json",
            mcp_config_path=tmp_path / "m.json",
            plugin_dir=tmp_path / "plugin",
            append_system_prompt="Use Fusion",
            allowed_tools=["mcp__fusion"],
        ),
    )
    assert with_plugin[with_plugin.index("--plugin-dir") + 1] == str(tmp_path / "plugin")
    assert with_plugin[with_plugin.index("--append-system-prompt") + 1] == "Use Fusion"
    assert with_plugin[with_plugin.index("--allowedTools") + 1] == "mcp__fusion"


# -- arms, tasks and prompts ----------------------------------------------------------------------


def test_default_arm_matrix_is_three_executors_with_and_without_fusion() -> None:
    names = [a.name for a in arm_matrix()]
    assert names == ["opus", "opus+fusion", "sonnet", "sonnet+fusion", "haiku", "haiku+fusion"]
    assert all(a.fusion == a.name.endswith("+fusion") for a in arm_matrix())


def test_parse_arms_from_options() -> None:
    assert [a.name for a in parse_e2e_arms("haiku", "on")] == ["haiku+fusion"]
    assert [a.name for a in parse_e2e_arms("opus,haiku", "off")] == ["opus", "haiku"]
    assert len(parse_e2e_arms(None, "both")) == 6
    with pytest.raises(ValueError, match="unknown executor"):
        parse_e2e_arms("gpt", "both")
    with pytest.raises(ValueError, match="--fusion"):
        parse_e2e_arms("opus", "maybe")


def test_only_executable_coding_tasks_are_used(dataset: Path) -> None:
    cfg = _config(dataset, arm_matrix(fusion=(False,)))
    assert [t.id for t in e2e_tasks(cfg)] == ["tiny-add"]


def test_prompt_asks_for_edits_not_a_patch(dataset: Path) -> None:
    task = next(t for t in load_dataset(dataset) if t.id == "tiny-add")
    prompt = e2e_prompt(task)
    assert prompt.startswith("Fix `add` in calc.py")
    assert "patch" not in prompt.lower()
    assert "Edit the files in the current directory" in prompt
    assert "Do not edit or delete the tests" in prompt


# -- metrics --------------------------------------------------------------------------------------


def test_fusion_ledger_counts_runs_and_sums_cost(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    assert fusion_ledger(db) == (0, 0.0)  # no server ever wrote: nothing was spent
    store = RunStore(db_path=str(db))
    for cost in (0.01, 0.02):
        run_id = store.create_run(task_type="ask", input_data={}, sanitized_input={})
        store.complete_run(
            run_id,
            status="completed",
            output_data={},
            trace={},
            total_cost_usd=cost,
            total_latency_ms=10.0,
            steps=[],
        )
    store.close()
    assert fusion_ledger(db) == (2, pytest.approx(0.03))


def test_total_cost_is_host_plus_fusion() -> None:
    metrics = E2EMetrics(
        executor="haiku",
        model_id="claude-haiku-5-5",
        fusion=True,
        host_cost_usd=0.12,
        fusion_cost_usd=0.03,
        wall_seconds=50.0,
        turns=7,
        tool_calls=4,
        fusion_calls=2,
        fusion_tool_calls=1,
        tests_passed=3,
        tests_expected=3,
    )
    assert metrics.total_cost_usd == pytest.approx(0.15)
    assert metrics.solved
    bench = bench_metrics(metrics, eval_seconds=1.5)
    assert bench.cost_usd == pytest.approx(0.15)
    assert bench.cost_known
    assert bench.solved
    assert bench.quality == 1.0
    assert bench.seconds_to_complete == 50.0
    assert bench.eval_seconds == 1.5
    assert bench.calls == 7 + 2  # host turns plus Fusion runs


def test_unknown_host_cost_is_not_counted_as_free() -> None:
    metrics = E2EMetrics(
        executor="opus",
        model_id="claude-opus-5-5",
        fusion=False,
        host_cost_usd=None,
        wall_seconds=1.0,
        tests_expected=3,
    )
    assert not bench_metrics(metrics).cost_known
    assert not metrics.solved


# -- the runner, against a fake `claude` on PATH --------------------------------------------------


def test_run_with_fake_claude_scores_hidden_tests_and_keeps_the_source(
    dataset: Path, fake_bin: Path, tmp_path: Path
) -> None:
    log = tmp_path / "argv.jsonl"
    _fake_claude(fake_bin, events=_stream(), writes={"calc.py": FIXED}, log=log)
    before = _tree_hash(dataset)
    ledger = SpendLedger(tmp_path / "spend.json")
    env = _env(tmp_path, spend=ledger)
    run = run_e2e(_config(dataset, [E2EArm(name="haiku", executor="haiku", fusion=False)]), env)

    assert _tree_hash(dataset) == before  # the task's source is byte-identical after the run
    assert run.status == "completed"
    [item] = run.items
    assert item.status == "completed", item.error
    assert item.metrics.solved
    assert item.metrics.quality == 1.0
    assert item.metrics.cost_usd == pytest.approx(_haiku_cost())
    assert item.e2e is not None
    assert item.e2e["host_cost_usd"] == pytest.approx(_haiku_cost())
    assert item.e2e["host_cost_reported_usd"] == pytest.approx(0.12)  # Claude Code's own figure
    assert item.e2e["fusion_cost_usd"] == 0.0
    assert item.e2e["tool_calls"] == 3
    assert item.e2e["turns"] == 7
    assert item.e2e["files_changed"] == ["calc.py"]
    # the spend ledger got the run's cost; the store has the item
    assert ledger.total() == pytest.approx(_haiku_cost())
    assert [i.job_key for i in env.store.items(run.run_id)] == [item.job_key]
    argv = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert "--plugin-dir" not in argv


def test_unfixed_code_fails_the_hidden_tests(dataset: Path, fake_bin: Path, tmp_path: Path) -> None:
    _fake_claude(fake_bin, events=_stream(), log=tmp_path / "argv.jsonl")
    run = run_e2e(
        _config(dataset, [E2EArm(name="opus", executor="opus", fusion=False)]), _env(tmp_path)
    )
    [item] = run.items
    assert not item.metrics.solved
    assert item.metrics.quality is not None
    assert item.metrics.quality < 1.0
    assert item.e2e is not None
    assert item.e2e["files_changed"] == []


def test_fusion_arm_reads_cost_from_fusion_ledger_and_loads_plugin(
    dataset: Path, fake_bin: Path, tmp_path: Path
) -> None:
    plugin = tmp_path / "plugin"
    _write(plugin / ".claude-plugin" / "plugin.json", '{"name": "fusion"}')
    _write(plugin / ".mcp.json", '{"mcpServers": {"fusion": {"command": "uvx"}}}')
    _write(plugin / "commands" / "ask.md", "allowed-tools: mcp__plugin_fusion_fusion__fusion_ask\n")
    log = tmp_path / "argv.jsonl"
    _fake_claude(
        fake_bin,
        events=_stream(fusion=True),
        writes={"calc.py": FIXED},
        fusion_runs=[0.01, 0.02],
        log=log,
    )
    env = _env(tmp_path, plugin=plugin)
    run = run_e2e(
        _config(dataset, [E2EArm(name="haiku+fusion", executor="haiku", fusion=True)]), env
    )
    [item] = run.items
    assert item.status == "completed", item.error
    assert item.e2e is not None
    assert item.e2e["fusion_calls"] == 2
    assert item.e2e["fusion_tool_calls"] == 1
    assert item.e2e["fusion_cost_usd"] == pytest.approx(0.03)
    assert item.metrics.cost_usd == pytest.approx(_haiku_cost() + 0.03)
    argv = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    plugin_copy = Path(argv[argv.index("--plugin-dir") + 1])
    assert plugin_copy != plugin  # a copy in the sandbox, removed with it
    assert not plugin_copy.exists()
    assert "--append-system-prompt" in argv
    assert (plugin / ".mcp.json").exists()  # the source plugin is untouched


def test_fusion_arm_without_a_connected_server_is_an_error(
    dataset: Path, fake_bin: Path, tmp_path: Path
) -> None:
    _fake_claude(fake_bin, events=_stream(fusion=False), log=tmp_path / "argv.jsonl")
    run = run_e2e(
        _config(dataset, [E2EArm(name="haiku+fusion", executor="haiku", fusion=True)]),
        _env(tmp_path),
    )
    [item] = run.items
    assert item.status == "error"
    assert "did not connect" in (item.error or "")


def test_spend_is_checked_before_each_run(dataset: Path, fake_bin: Path, tmp_path: Path) -> None:
    log = tmp_path / "argv.jsonl"
    _fake_claude(fake_bin, events=_stream(), log=log)
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=1.0)
    ledger.append(0.9, task="earlier", purpose="filling the budget")
    run = run_e2e(
        _config(dataset, [E2EArm(name="opus", executor="opus", fusion=False)], run_budget_usd=0.5),
        _env(tmp_path, spend=ledger),
    )
    assert run.status == "stopped"
    assert "live-spend cap" in (run.stop_reason or "")
    assert run.items == []
    assert not log.exists()  # claude was never started


def test_run_cap_stops_the_study(dataset: Path, fake_bin: Path, tmp_path: Path) -> None:
    _fake_claude(fake_bin, events=_stream(), log=tmp_path / "argv.jsonl")
    run = run_e2e(
        _config(
            dataset,
            [E2EArm(name="opus", executor="opus", fusion=False)],
            max_usd=0.4,
            run_budget_usd=0.5,
        ),
        _env(tmp_path),
    )
    assert run.status == "stopped"
    assert "max_usd" in (run.stop_reason or "")


def test_resume_skips_finished_jobs(dataset: Path, fake_bin: Path, tmp_path: Path) -> None:
    log = tmp_path / "argv.jsonl"
    _fake_claude(fake_bin, events=_stream(), writes={"calc.py": FIXED}, log=log)
    env = _env(tmp_path)
    cfg = _config(dataset, [E2EArm(name="haiku", executor="haiku", fusion=False)])
    first = run_e2e(cfg, env)
    again = run_e2e(cfg, env, run_id=first.run_id)
    assert again.resumed_jobs == 1
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


# -- mock mode and the CLI ------------------------------------------------------------------------


def test_mock_recording_is_deterministic_and_priced_from_the_catalog(dataset: Path) -> None:
    task = next(t for t in load_dataset(dataset) if t.id == "tiny-add")
    registry = ModelRegistry.for_mode(use_mock=False)
    arm = E2EArm(name="haiku+fusion", executor="haiku", fusion=True)
    one = mock_recording(task, arm, "claude-haiku-5-5", seed=0, registry=registry)
    assert one == mock_recording(task, arm, "claude-haiku-5-5", seed=0, registry=registry)
    result = one["events"][-1]
    assert result["type"] == "result"
    assert result["total_cost_usd"] > 0
    assert one["fusion_runs"]  # a Fusion arm calls Fusion


def test_cli_mock_produces_a_report(
    dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_BENCH_DIR", str(tmp_path / "results"))
    before = _tree_hash(dataset)
    result = cli.invoke(
        app, ["bench", "e2e", "--mock", "--dataset", str(dataset), "--split", "all"]
    )
    assert result.exit_code == 0, result.output
    assert "Claude Code end to end" in result.output
    assert "haiku+fusion" in result.output
    assert _tree_hash(dataset) == before
    runs = BenchStore(tmp_path / "results").list_runs()
    assert len(runs) == 1
    run_id = runs[0].run_id
    assert len(BenchStore(tmp_path / "results").items(run_id)) == 6
    report = cli.invoke(app, ["bench", "report", run_id])
    assert report.exit_code == 0, report.output
    assert "Claude Code end to end" in report.output
    assert "opus+fusion" in report.output
    assert "**Arm haiku+fusion:** claude-code haiku + fusion, model claude-haiku" in report.output
    html = cli.invoke(app, ["bench", "report", run_id, "--format", "html"])
    assert html.exit_code == 0, html.output
    assert "Claude Code end to end" in (
        BenchStore(tmp_path / "results").run_dir(run_id) / "report.html"
    ).read_text(encoding="utf-8")


def test_cli_live_needs_a_budget_and_an_api_key(
    dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_BENCH_DIR", str(tmp_path / "results"))
    no_cap = cli.invoke(app, ["bench", "e2e", "--dataset", str(dataset)])
    assert no_cap.exit_code != 0
    assert "--max-usd" in no_cap.output
    no_key = cli.invoke(app, ["bench", "e2e", "--dataset", str(dataset), "--max-usd", "1"])
    assert no_key.exit_code != 0
    assert "ANTHROPIC_API_KEY" in no_key.output


def test_bench_resume_refuses_an_e2e_run(
    dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_BENCH_DIR", str(tmp_path / "results"))
    done = cli.invoke(
        app,
        [
            "bench",
            "e2e",
            "--mock",
            "--dataset",
            str(dataset),
            "--split",
            "all",
            "--executors",
            "haiku",
            "--fusion",
            "off",
        ],
    )
    assert done.exit_code == 0, done.output
    run_id = BenchStore(tmp_path / "results").list_runs()[0].run_id
    resumed = cli.invoke(app, ["bench", "resume", run_id])
    assert resumed.exit_code != 0
    assert "fusion bench e2e --run-id" in resumed.output


def test_fusion_db_is_inside_the_sandbox(dataset: Path, fake_bin: Path, tmp_path: Path) -> None:
    """The Fusion server writes its ledger beside the copy, never to the user's run history."""
    _fake_claude(
        fake_bin, events=_stream(fusion=True), fusion_runs=[0.01], log=tmp_path / "argv.jsonl"
    )
    run_e2e(
        _config(dataset, [E2EArm(name="haiku+fusion", executor="haiku", fusion=True)]),
        _env(tmp_path),
    )
    user_db = Path(os.environ["FUSION_DATA_DIR"]) / "runs.db"
    if user_db.exists():
        with sqlite3.connect(user_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_a_scoring_failure_keeps_the_run_and_its_cost(
    dataset: Path, fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fusion.bench.e2e import runner as e2e_runner
    from fusion.bench.sandbox import SandboxError

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise SandboxError("no sandbox today")

    monkeypatch.setattr(e2e_runner, "run_hidden_tests", broken)
    _fake_claude(fake_bin, events=_stream(), writes={"calc.py": FIXED}, log=tmp_path / "a.jsonl")
    ledger = SpendLedger(tmp_path / "spend.json")
    run = run_e2e(
        _config(dataset, [E2EArm(name="haiku", executor="haiku", fusion=False)]),
        _env(tmp_path, spend=ledger),
    )
    [item] = run.items
    assert item.status == "error"
    assert "no sandbox today" in (item.error or "")
    assert item.metrics.cost_usd == pytest.approx(_haiku_cost())
    assert ledger.total() == pytest.approx(_haiku_cost())  # spent, so it is on the ledger


# -- host cost at catalog prices, and what a run leaves behind -------------------------------------


def test_host_usage_is_priced_from_the_catalog() -> None:
    usage = _result_event()["modelUsage"]
    cost, unpriced = price_host_usage(usage, CATALOG)
    assert cost == pytest.approx(_haiku_cost())
    assert unpriced == []


def test_a_model_the_catalog_lacks_falls_back_to_claude_codes_figure() -> None:
    usage = {
        "claude-haiku-5-5-20261007": {"inputTokens": 1_000, "outputTokens": 0, "costUSD": 9.0},
        "some-internal-model": {"inputTokens": 50, "outputTokens": 5, "costUSD": 0.001},
    }
    cost, unpriced = price_host_usage(usage, CATALOG)
    price = CATALOG["claude-haiku"].price_at()
    assert price is not None
    assert cost == pytest.approx(1_000 * price.input_per_1m / 1_000_000 + 0.001)  # dated id ok
    assert unpriced == ["some-internal-model"]
    assert price_host_usage({}, CATALOG) == (None, [])
    assert price_host_usage({"x": {"inputTokens": 5}}, CATALOG) == (None, ["x"])  # no figure


def test_unrecognized_model_is_flagged_and_the_transcript_kept(
    dataset: Path, fake_bin: Path, tmp_path: Path
) -> None:
    events = _stream()
    events.insert(
        1,
        {
            "type": "system",
            "subtype": "warning",
            "message": '[claude-code:unrecognized_model] {"model":"claude-haiku-5-5"}',
        },
    )
    _fake_claude(fake_bin, events=events, writes={"calc.py": FIXED}, log=tmp_path / "a.jsonl")
    env = _env(tmp_path)
    run = run_e2e(_config(dataset, [E2EArm(name="haiku", executor="haiku", fusion=False)]), env)
    [item] = run.items
    assert any("does not know the model claude-haiku-5-5" in w for w in item.warnings)
    transcript = env.store.run_dir(run.run_id) / "evidence" / "e2e" / f"{item.job_key}.jsonl"
    assert transcript.is_file()
    assert "unrecognized_model" in transcript.read_text(encoding="utf-8")
    assert item.e2e is not None
    assert item.e2e["host_usage"]["claude-haiku-5-5"]["inputTokens"] == 1_000
