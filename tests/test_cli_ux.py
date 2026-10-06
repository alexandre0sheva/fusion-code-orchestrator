"""The command line's contract: one front door, stdout for results, stderr for the rest, exit codes.

Every run command is exercised on the offline mock provider. Nothing here touches a real provider.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from typer.testing import CliRunner

from fusion.cli.main import app
from fusion.mcp_server.schemas import FusionToolResult

DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
LONG_QUESTION = "How should I retry a failed HTTP call without hammering the server?"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def parse(result: Any) -> Any:
    """stdout alone must be the JSON document, whatever went to stderr."""
    return json.loads(result.stdout)


# -- ask: the front door --------------------------------------------------------------------------


def test_ask_prints_the_answer_on_stdout_and_progress_on_stderr(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ask", LONG_QUESTION, "--mock"])
    assert result.exit_code == 0, result.output
    assert "## Fusion Answer" in result.stdout and "panel:" not in result.stdout
    assert "panel: asking 2 models" in result.stderr and "synthesizing" in result.stderr


def test_ask_json_is_the_mcp_tool_record(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ask", LONG_QUESTION, "--mock", "--json"])
    assert result.exit_code == 0, result.output
    record = parse(result)
    jsonschema.validate(record, FusionToolResult.model_json_schema())
    assert record["run_id"].startswith("run_") and "claims" not in record  # compact by default
    full = parse(runner.invoke(app, ["ask", LONG_QUESTION, "--mock", "--json", "--detail", "full"]))
    assert "claims" in full and "usage" in full


def test_ask_attaches_files_and_background(runner: CliRunner, tmp_path: Path) -> None:
    source = tmp_path / "http.py"
    source.write_text("def get(): ...\n")
    context = tmp_path / "ctx.md"
    context.write_text("We use httpx.")
    result = runner.invoke(
        app,
        [
            "ask",
            LONG_QUESTION,
            "-f",
            str(source),
            "--context-file",
            str(context),
            "--context",
            "Python 3.12",
            "--mock",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    stored = runner.invoke(app, ["runs", "show", parse(result)["run_id"]])
    assert stored.exit_code == 0 and "http.py" in stored.stdout


def test_ask_reads_the_question_from_stdin(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ask", "-", "--mock", "--json"], input=LONG_QUESTION)
    assert result.exit_code == 0 and parse(result)["run_id"]


def test_quiet_removes_progress_but_not_the_answer(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ask", LONG_QUESTION, "--mock", "-q"])
    assert result.exit_code == 0 and result.stderr == "" and "## Fusion Answer" in result.stdout


def test_a_run_with_no_answer_exits_3(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ask", "hi", "--mock", "--json"])
    assert result.exit_code == 3
    assert parse(result)["halted"] == "insufficient_context"


# -- the other run commands share its flags -------------------------------------------------------


def test_review_reads_a_diff_from_a_file_or_stdin(runner: CliRunner, tmp_path: Path) -> None:
    path = tmp_path / "change.diff"
    path.write_text(DIFF)
    from_file = runner.invoke(app, ["review-diff", "--file", str(path), "--mock", "--json"])
    from_stdin = runner.invoke(app, ["review-diff", "-f", "-", "--mock", "--json"], input=DIFF)
    assert from_file.exit_code == from_stdin.exit_code == 0
    assert parse(from_file)["strategy"] == parse(from_stdin)["strategy"]


def test_debug_takes_an_argument_a_file_or_the_old_flags(runner: CliRunner, tmp_path: Path) -> None:
    trace = tmp_path / "trace.txt"
    trace.write_text("TimeoutError: connection to db timed out after 30s\n  at pool.py:88")
    logs = tmp_path / "app.log"
    logs.write_text("retrying... retrying...")
    for args in (
        ["debug", "TimeoutError: connection to db timed out after 30s in pool.py line 88"],
        ["debug", "--error", "TimeoutError: connection to db timed out after 30s in pool.py"],
        ["debug", "--file", str(trace), "--logs-file", str(logs)],
        ["debug", "--error-file", str(trace)],  # the v0.1 spelling still works
    ):
        result = runner.invoke(app, [*args, "--mock", "--json", "-q"])
        assert result.exit_code == 0, (args, result.output)
        assert parse(result)["run_id"]


def test_decide_and_plan_accept_text_or_files(runner: CliRunner, tmp_path: Path) -> None:
    feature = tmp_path / "feature.md"
    feature.write_text("Add rate limiting to the public API, per API key, with a sliding window.")
    cases = [
        ["decide", "Redis or Postgres for a job queue with ten workers?"],
        ["decide", "--question", "Redis or Postgres for a job queue with ten workers?"],
        ["decide", "x", "--constraints", "one small team"],
        ["plan", "Add rate limiting to the public API, per API key, with a sliding window."],
        ["plan", "--feature-file", str(feature)],  # the v0.1 spelling still works
        ["plan", "-f", str(feature), "--constraints", "no new dependencies"],
    ]
    for args in cases:
        result = runner.invoke(app, [*args, "--mock", "--json", "-q"])
        assert result.exit_code in {0, 3}, (args, result.output)
        assert "run_id" in parse(result)


def test_eval_answer_scores_an_answer(runner: CliRunner, tmp_path: Path) -> None:
    question = tmp_path / "q.md"
    question.write_text("Explain exponential backoff in one paragraph.")
    answer = tmp_path / "a.md"
    answer.write_text("Wait longer after each failure, doubling the delay, with jitter.")
    result = runner.invoke(
        app,
        [
            "eval-answer",
            "--question-file",
            str(question),
            "--answer-file",
            str(answer),
            "--mock",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert parse(result)["run_id"]


def test_compare_claude_runs_prints_json(runner: CliRunner, tmp_path: Path) -> None:
    for name in ("task", "opus", "fusion"):
        (tmp_path / f"{name}.md").write_text(f"{name} text, long enough to be judged")
    result = runner.invoke(
        app,
        [
            "compare-claude-runs",
            "--task-file",
            str(tmp_path / "task.md"),
            "--opus-file",
            str(tmp_path / "opus.md"),
            "--fusion-file",
            str(tmp_path / "fusion.md"),
            "--mock",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert {"result", "evals", "display_markdown"} <= set(parse(result))


# -- exit codes -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [["ask"], ["review-diff"], ["debug"], ["decide"], ["plan"], ["ask", "x", "--max-cost", "0"]],
)
def test_a_missing_input_is_a_usage_error_with_exit_code_2(
    runner: CliRunner, args: list[str]
) -> None:
    result = runner.invoke(app, [*args, "--mock"])
    assert result.exit_code == 2 and "Error:" in result.stderr and "Traceback" not in result.output


def test_an_unknown_option_is_exit_code_2(runner: CliRunner) -> None:
    assert runner.invoke(app, ["ask", "x", "--nope"]).exit_code == 2


def test_a_missing_file_is_one_line_and_exit_code_1(runner: CliRunner) -> None:
    result = runner.invoke(app, ["review-diff", "--file", "/nowhere/x.diff", "--mock"])
    assert result.exit_code == 1
    assert result.stderr.strip() == "Error: a diff not found: /nowhere/x.diff"


def test_no_provider_key_is_an_actionable_error_before_any_work(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FUSION_DEFAULT_PROVIDER")
    result = runner.invoke(app, ["ask", LONG_QUESTION])
    assert result.exit_code == 1
    assert "No provider API key is set" in result.stderr
    assert "fusion doctor" in result.stderr and "--mock" in result.stderr
    assert result.stdout == ""


def test_one_key_is_enough_to_start(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    from fusion.cli.common import require_a_provider

    monkeypatch.delenv("FUSION_DEFAULT_PROVIDER")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    require_a_provider(False)  # no exception


# -- friendly errors ------------------------------------------------------------------------------


def test_an_unexpected_failure_is_one_line_and_verbose_shows_the_traceback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the database is on fire")

    monkeypatch.setattr("fusion.cli.run_cmds.make_tools", explode)
    quiet = runner.invoke(app, ["ask", LONG_QUESTION, "--mock"])
    assert quiet.exit_code == 1
    assert "Error: the database is on fire" in quiet.stderr and "--verbose" in quiet.stderr
    assert "Traceback" not in quiet.output
    loud = runner.invoke(app, ["--verbose", "ask", LONG_QUESTION, "--mock"])
    assert isinstance(loud.exception, RuntimeError)


def test_a_bad_config_file_is_a_message_not_a_traceback(
    runner: CliRunner, fusion_home: Path
) -> None:
    (fusion_home / "config" / "config.yaml").write_text("fanout: [unclosed")
    result = runner.invoke(app, ["config", "show", "--resolved"])
    assert result.exit_code == 1 and "Traceback" not in result.output and result.stderr


def test_a_missing_run_is_exit_code_1_with_a_hint(runner: CliRunner) -> None:
    result = runner.invoke(app, ["runs", "show", "run_nope"])
    assert result.exit_code == 1
    assert "Run not found: run_nope" in result.stderr and "fusion runs list" in result.stderr


# -- --json everywhere ----------------------------------------------------------------------------

ROW = {"type": "object", "required": ["run_id", "task_type", "status", "cost_usd", "latency_ms"]}


def test_json_output_of_the_inspection_commands(runner: CliRunner) -> None:
    runner.invoke(app, ["ask", LONG_QUESTION, "--mock", "-q"])
    schemas: dict[tuple[str, ...], dict[str, Any]] = {
        ("runs", "list"): {"type": "array", "minItems": 1, "items": ROW},
        ("runs", "costs"): {
            "type": "object",
            "required": ["runs", "total_cost_usd", "avg_cost_usd", "avg_latency_ms"],
        },
        ("config", "paths"): {
            "type": "array",
            "items": {"type": "object", "required": ["name", "path", "exists"]},
        },
        ("config", "validate"): {
            "type": "object",
            "required": ["valid", "errors", "warnings"],
            "properties": {"valid": {"type": "boolean"}},
        },
        ("version",): {"type": "object", "required": ["name", "version"]},
        ("models", "list"): {
            "type": "array",
            "minItems": 1,
            "items": {"type": "object", "required": ["alias", "provider", "model_id", "price"]},
        },
        ("models", "check"): {"type": "object", "required": ["warnings", "live", "problems"]},
        ("strategies", "list"): {"type": "array", "minItems": 1},
        ("stats",): {"type": "object"},
    }
    for command, schema in schemas.items():
        result = runner.invoke(app, [*command, "--json"])
        assert result.exit_code == 0, (command, result.output)
        jsonschema.validate(parse(result), schema)


def test_runs_export_is_one_json_object_per_line(runner: CliRunner) -> None:
    runner.invoke(app, ["ask", LONG_QUESTION, "--mock", "-q"])
    lines = runner.invoke(app, ["runs", "export"]).stdout.strip().splitlines()
    assert lines and all(json.loads(line)["run_id"] for line in lines)


# -- deprecations ---------------------------------------------------------------------------------


def test_legacy_commands_warn_on_stderr_and_still_work(runner: CliRunner, tmp_path: Path) -> None:
    runner.invoke(app, ["ask", LONG_QUESTION, "--mock", "-q"])
    listed = runner.invoke(app, ["list-runs"])
    assert listed.exit_code == 0 and "Recent Runs" in listed.stdout
    assert "`fusion list-runs` will be removed in 0.3.0; use `fusion runs list`" in listed.stderr

    mocked = runner.invoke(app, ["run-mock"])
    assert mocked.exit_code == 0 and "Run ID" in mocked.stdout and "run-mock" in mocked.stderr

    diff = tmp_path / "x.diff"
    diff.write_text(DIFF)
    reviewed = runner.invoke(app, ["review", str(diff)])
    assert reviewed.exit_code == 0 and "review-diff --file DIFF" in reviewed.stderr

    gone = runner.invoke(
        app,
        [
            "compare-cost",
            "--fusion-run-id",
            "run_nope",
            "--opus-input-tokens",
            "1",
            "--opus-output-tokens",
            "1",
        ],
    )
    assert gone.exit_code == 1 and "compare-cost" in gone.stderr


def test_the_current_commands_do_not_warn(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ask", LONG_QUESTION, "--mock", "-q"])
    assert "Deprecated" not in result.output


def test_help_names_every_deprecated_command_as_such(runner: CliRunner) -> None:
    text = runner.invoke(app, ["--help"], terminal_width=200).stdout
    for command in ("review", "run-mock", "list-runs", "compare-cost"):
        line = next(ln for ln in text.splitlines() if re.search(rf"│ {command}\s", ln))
        assert "Deprecated" in line


# -- the old import path keeps working ------------------------------------------------------------


def test_the_old_module_path_still_exposes_app() -> None:
    from fusion.cli.app import app as legacy

    assert legacy is app
