"""Commands that run the panel: ask, review-diff, debug, decide, plan, digest, patch and
eval-answer.

They share one shape: the main input is an argument, ``--file`` or stdin; then ``--context``,
``--strategy``, ``--max-cost``, ``--detail``, ``--json``, ``--mock``, ``--db-path`` and ``--quiet``.
The answer goes to stdout (Markdown, or the same record the MCP tool returns with ``--json``),
progress and warnings to stderr. Exit codes are listed in ``fusion.cli.common``.
"""

from __future__ import annotations

import enum
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markdown import Markdown

from fusion.cli.common import (
    EXIT_HALTED,
    EXIT_USAGE,
    CliError,
    ContextFileOption,
    ContextOption,
    DbPathOption,
    Detail,
    DetailOption,
    JsonOption,
    MaxCostOption,
    MockOption,
    QuietOption,
    StrategyOption,
    check_max_cost,
    console,
    context_text,
    echo_json,
    err_console,
    make_tools,
    primary_text,
    read_file,
    require_a_provider,
    run_tool,
)
from fusion.mcp_server.patch import present_patch
from fusion.mcp_server.response import (
    COMPACT_MAX_TOKENS,
    DIGEST_OVERHEAD_TOKENS,
    present_comparison,
    present_run,
)
from fusion.mcp_server.schemas import (
    CompareClaudeRunsInput,
    DebugErrorInput,
    DecideArchitectureInput,
    DigestInput,
    EvalAnswerInput,
    FusionAskInput,
    PlanFeatureInput,
    ProposePatchInput,
    ReviewDiffInput,
)

FileOption = Annotated[
    list[Path] | None,
    typer.Option(
        "--file",
        "-f",
        help="A project file for the panel to read (repeatable). Read like the MCP `paths`: "
        "under the current directory, skipping secret, ignored, binary and oversize files",
    ),
]
GlobOption = Annotated[
    list[str] | None,
    typer.Option("--glob", help="Read the project files matching a glob, e.g. 'src/**/*.py'"),
]
DiffRefOption = Annotated[
    str | None,
    typer.Option("--diff-ref", help="Read `git diff <REF>` itself, e.g. HEAD~1 or main...HEAD"),
]


def emit_run(
    output: dict[str, Any],
    *,
    detail: Detail,
    as_json: bool,
    quiet: bool,
    max_tokens: int = COMPACT_MAX_TOKENS,
) -> None:
    """Print a finished run (stdout) and exit 3 if it has no answer."""
    result = present_run(output, detail.value, max_tokens=max_tokens)
    if as_json:
        echo_json(result.model_dump(exclude_none=True))
    else:
        text = result.display_markdown
        if sys.stdout.isatty():
            console.print(Markdown(text))
        else:
            typer.echo(text)
        if not quiet:
            err_console.print(
                f"run {result.run_id}  (fusion runs show {result.run_id})",
                style="dim",
                highlight=False,
                soft_wrap=True,
            )
    if result.halted:
        raise typer.Exit(EXIT_HALTED)


def _go(
    call: Any,
    *,
    mock: bool,
    db_path: str | None,
    detail: Detail,
    as_json: bool,
    quiet: bool,
    max_tokens: int = COMPACT_MAX_TOKENS,
) -> None:
    require_a_provider(mock)
    tools = make_tools(db_path, mock)
    output = run_tool(tools, call, quiet=quiet)
    emit_run(output, detail=detail, as_json=as_json, quiet=quiet, max_tokens=max_tokens)


def ask(
    prompt: Annotated[
        str | None, typer.Argument(help="The question, stated so it stands alone (- reads stdin)")
    ] = None,
    file: FileOption = None,
    context: ContextOption = "",
    context_file: ContextFileOption = None,
    strategy: StrategyOption = None,
    max_cost: MaxCostOption = None,
    detail: DetailOption = Detail.compact,
    as_json: JsonOption = False,
    mock: MockOption = False,
    db_path: DbPathOption = None,
    quiet: QuietOption = False,
) -> None:
    """Ask the panel a coding question and print one answer with its cost.

    Example: fusion ask "Why does this retry loop never back off?" --file src/http.py
    """
    question = primary_text(prompt, None, what="the question", flag="the PROMPT argument")
    request = FusionAskInput(
        prompt=question,
        context=context_text(context, context_file),
        paths=[str(path) for path in file or []],
        strategy=strategy,
        max_cost_usd=check_max_cost(max_cost),
        detail=detail.value,
    )
    _go(
        lambda tools: tools.fusion_ask(request),
        mock=mock,
        db_path=db_path,
        detail=detail,
        as_json=as_json,
        quiet=quiet,
    )


def review_diff(
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="The diff file (- reads stdin)")
    ] = None,
    diff_ref: DiffRefOption = None,
    goals: Annotated[str, typer.Option(help="What to focus on, such as security")] = "",
    context: ContextOption = "",
    context_file: ContextFileOption = None,
    strategy: StrategyOption = None,
    max_cost: MaxCostOption = None,
    detail: DetailOption = Detail.compact,
    as_json: JsonOption = False,
    mock: MockOption = False,
    db_path: DbPathOption = None,
    quiet: QuietOption = False,
) -> None:
    """Review a diff with the panel.

    Examples: fusion review-diff --diff-ref main...HEAD
              git diff main | fusion review-diff --file -
    """
    diff = "" if diff_ref and file is None else primary_text(None, file, what="a diff")
    request = ReviewDiffInput(
        diff=diff,
        diff_ref=diff_ref,
        goals=goals,
        context=context_text(context, context_file),
        strategy=strategy,
        max_cost_usd=check_max_cost(max_cost),
        detail=detail.value,
    )
    _go(
        lambda tools: tools.fusion_review_diff(request),
        mock=mock,
        db_path=db_path,
        detail=detail,
        as_json=as_json,
        quiet=quiet,
    )


def debug(
    error_text: Annotated[
        str | None, typer.Argument(metavar="[ERROR]", help="The error message (- reads stdin)")
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option("--file", "-f", "--error-file", help="File with the error or stack trace"),
    ] = None,
    error: Annotated[str, typer.Option("--error", help="The error message (same as ERROR)")] = "",
    logs_file: Annotated[
        Path | None, typer.Option("--logs-file", help="Relevant log output")
    ] = None,
    context: ContextOption = "",
    context_file: ContextFileOption = None,
    strategy: StrategyOption = None,
    max_cost: MaxCostOption = None,
    detail: DetailOption = Detail.compact,
    as_json: JsonOption = False,
    mock: MockOption = False,
    db_path: DbPathOption = None,
    quiet: QuietOption = False,
) -> None:
    """Diagnose an error: ranked root causes and how to check each.

    Example: fusion debug --file trace.txt --logs-file app.log
    """
    message = primary_text(error_text or error or None, file, what="the error", flag="--file")
    request = DebugErrorInput(
        error_message=message,
        logs=read_file(logs_file, "logs file") if logs_file else "",
        context=context_text(context, context_file),
        strategy=strategy,
        max_cost_usd=check_max_cost(max_cost),
        detail=detail.value,
    )
    _go(
        lambda tools: tools.fusion_debug_error(request),
        mock=mock,
        db_path=db_path,
        detail=detail,
        as_json=as_json,
        quiet=quiet,
    )


def decide(
    question_text: Annotated[
        str | None, typer.Argument(metavar="[QUESTION]", help="The decision to make")
    ] = None,
    question: Annotated[
        str, typer.Option("--question", help="The decision to make (same as QUESTION)")
    ] = "",
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="File that holds the question")
    ] = None,
    constraints: Annotated[str, typer.Option(help="Constraints and requirements")] = "",
    context: ContextOption = "",
    context_file: ContextFileOption = None,
    strategy: StrategyOption = None,
    max_cost: MaxCostOption = None,
    detail: DetailOption = Detail.compact,
    as_json: JsonOption = False,
    mock: MockOption = False,
    db_path: DbPathOption = None,
    quiet: QuietOption = False,
) -> None:
    """Weigh an architecture decision.

    Example: fusion decide "Redis or Postgres for a job queue?" --constraints "one small team"
    """
    text = primary_text(question_text or question or None, file, what="the question")
    request = DecideArchitectureInput(
        question=text,
        constraints=constraints,
        context=context_text(context, context_file),
        strategy=strategy,
        max_cost_usd=check_max_cost(max_cost),
        detail=detail.value,
    )
    _go(
        lambda tools: tools.fusion_decide_architecture(request),
        mock=mock,
        db_path=db_path,
        detail=detail,
        as_json=as_json,
        quiet=quiet,
    )


def plan(
    feature_text: Annotated[
        str | None, typer.Argument(metavar="[FEATURE]", help="The feature to plan")
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option("--file", "-f", "--feature-file", help="File that describes the feature"),
    ] = None,
    constraints: Annotated[str, typer.Option(help="Constraints and requirements")] = "",
    context: ContextOption = "",
    context_file: ContextFileOption = None,
    strategy: StrategyOption = None,
    max_cost: MaxCostOption = None,
    detail: DetailOption = Detail.compact,
    as_json: JsonOption = False,
    mock: MockOption = False,
    db_path: DbPathOption = None,
    quiet: QuietOption = False,
) -> None:
    """Plan a multi-file feature.

    Example: fusion plan --file feature.md --constraints "no new dependencies"
    """
    feature = primary_text(feature_text, file, what="the feature")
    request = PlanFeatureInput(
        feature_description=feature,
        constraints=constraints,
        context=context_text(context, context_file),
        strategy=strategy,
        max_cost_usd=check_max_cost(max_cost),
        detail=detail.value,
    )
    _go(
        lambda tools: tools.fusion_plan_feature(request),
        mock=mock,
        db_path=db_path,
        detail=detail,
        as_json=as_json,
        quiet=quiet,
    )


def eval_answer(
    question_file: Annotated[
        Path | None, typer.Option("--question-file", help="The question or task")
    ] = None,
    answer_file: Annotated[
        Path | None, typer.Option("--answer-file", "-f", help="The answer to score")
    ] = None,
    rubric: Annotated[str, typer.Option(help="What a good answer must do")] = "",
    context: ContextOption = "",
    context_file: ContextFileOption = None,
    detail: DetailOption = Detail.compact,
    as_json: JsonOption = False,
    mock: MockOption = False,
    db_path: DbPathOption = None,
    quiet: QuietOption = False,
) -> None:
    """Score an answer against a rubric with the panel.

    Example: fusion eval-answer --question-file q.md --answer-file a.md
    """
    if answer_file is None:
        msg = "give the answer with --answer-file (use - for stdin)"
        raise typer.BadParameter(msg, param_hint="--answer-file")
    request = EvalAnswerInput(
        question=read_file(question_file, "question file") if question_file else "",
        answer=read_file(answer_file, "answer file"),
        rubric=rubric,
        context=context_text(context, context_file),
        detail=detail.value,
    )
    _go(
        lambda tools: tools.fusion_eval_answer(request),
        mock=mock,
        db_path=db_path,
        detail=detail,
        as_json=as_json,
        quiet=quiet,
    )


def compare_claude_runs(
    task_file: Annotated[Path, typer.Option("--task-file", help="Original prompt/task file")],
    opus_file: Annotated[Path, typer.Option("--opus-file", help="Claude Code + Opus output")],
    fusion_file: Annotated[Path, typer.Option("--fusion-file", help="Claude Code + Fusion output")],
    context_file: ContextFileOption = None,
    opus_cost: Annotated[float | None, typer.Option(help="Measured Opus cost USD")] = None,
    fusion_cost: Annotated[float | None, typer.Option(help="Measured Fusion cost USD")] = None,
    opus_latency_ms: Annotated[int | None, typer.Option(help="Measured Opus latency ms")] = None,
    fusion_latency_ms: Annotated[
        int | None, typer.Option(help="Measured Fusion latency ms")
    ] = None,
    as_json: JsonOption = False,
    mock: MockOption = False,
    db_path: DbPathOption = None,
    quiet: QuietOption = False,
) -> None:
    """Compare Claude Code + Opus output against Claude Code + Fusion output."""
    request = CompareClaudeRunsInput(
        task_prompt=read_file(task_file, "task file"),
        opus_output=read_file(opus_file, "Opus output file"),
        fusion_output=read_file(fusion_file, "Fusion output file"),
        context=read_file(context_file, "context file") if context_file else "",
        opus_cost_usd=opus_cost,
        fusion_cost_usd=fusion_cost,
        opus_latency_ms=opus_latency_ms,
        fusion_latency_ms=fusion_latency_ms,
    )
    require_a_provider(mock)
    tools = make_tools(db_path, mock)
    output = run_tool(tools, lambda t: t.fusion_compare_claude_runs(request), quiet=quiet)
    result = present_comparison(output)
    if as_json:
        echo_json(result.model_dump(exclude_none=True))
    else:
        typer.echo(result.display_markdown)


def digest(
    question: Annotated[
        str | None, typer.Argument(help="What you want to know about the files (- reads stdin)")
    ] = None,
    file: FileOption = None,
    glob: GlobOption = None,
    diff_ref: DiffRefOption = None,
    max_answer_tokens: Annotated[
        int, typer.Option("--max-answer-tokens", min=50, max=4000, help="Longest answer, tokens")
    ] = 600,
    context: ContextOption = "",
    strategy: StrategyOption = None,
    max_cost: MaxCostOption = None,
    detail: DetailOption = Detail.compact,
    as_json: JsonOption = False,
    mock: MockOption = False,
    db_path: DbPathOption = None,
    quiet: QuietOption = False,
) -> None:
    """Have cheap models read project files and answer one question briefly.

    Example: fusion digest "Where are retries handled?" --glob 'src/**/*.py'
    """
    text = primary_text(question, None, what="the question", flag="the QUESTION argument")
    if not (file or glob or diff_ref):
        msg = "name the material to read with --file, --glob or --diff-ref"
        raise CliError(msg, code=EXIT_USAGE)
    request = DigestInput(
        question=text,
        paths=[str(path) for path in file or []],
        globs=list(glob or []),
        diff_ref=diff_ref,
        max_answer_tokens=max_answer_tokens,
        context=context.strip(),
        strategy=strategy,
        max_cost_usd=check_max_cost(max_cost),
        detail=detail.value,
    )
    _go(
        lambda tools: tools.fusion_digest(request),
        mock=mock,
        db_path=db_path,
        detail=detail,
        as_json=as_json,
        quiet=quiet,
        max_tokens=max_answer_tokens + DIGEST_OVERHEAD_TOKENS,
    )


class Verify(enum.StrEnum):
    sandbox = "sandbox"
    host = "host"
    none = "none"


def patch(
    task: Annotated[
        str | None, typer.Argument(help="The change to make, stated so it stands alone (- stdin)")
    ] = None,
    file: FileOption = None,
    glob: GlobOption = None,
    diff_ref: DiffRefOption = None,
    tests_cmd: Annotated[
        str | None,
        typer.Option(
            "--tests-cmd",
            help="Shell command that runs the tests from the project root; in sandbox mode it "
            "runs only in the temporary copy, with no network",
        ),
    ] = None,
    verify: Annotated[
        Verify,
        typer.Option(
            help="sandbox: Fusion runs the checks in a temporary copy; host: you run them; "
            "none: no checks"
        ),
    ] = Verify.sandbox,
    diff_only: Annotated[
        bool,
        typer.Option("--diff-only", help="Print only the diff (exit 3 when there is none)"),
    ] = False,
    context: ContextOption = "",
    context_file: ContextFileOption = None,
    strategy: StrategyOption = None,
    max_cost: MaxCostOption = None,
    detail: DetailOption = Detail.compact,
    as_json: JsonOption = False,
    mock: MockOption = False,
    db_path: DbPathOption = None,
    quiet: QuietOption = False,
) -> None:
    """Get a patch, cross-checked by the panel's own checks, as a unified diff. Never applied.

    Example: fusion patch "Fix the off-by-one in paginate()" -f src/pages.py
    --tests-cmd 'python -m pytest -q tests/test_pages.py' --diff-only > fix.patch
    """
    text = primary_text(task, None, what="the change", flag="the TASK argument")
    if not (file or glob or diff_ref):
        msg = "name the code to change with --file, --glob or --diff-ref"
        raise CliError(msg, code=EXIT_USAGE)
    request = ProposePatchInput(
        task=text,
        paths=[str(path) for path in file or []],
        globs=list(glob or []),
        diff_ref=diff_ref,
        tests_cmd=tests_cmd,
        verify=verify.value,
        context=context_text(context, context_file),
        strategy=strategy,
        max_cost_usd=check_max_cost(max_cost),
        detail=detail.value,
    )
    require_a_provider(mock)
    tools = make_tools(db_path, mock)
    result = present_patch(run_tool(tools, lambda t: t.fusion_propose_patch(request), quiet=quiet))
    if diff_only:
        if result.patch:
            typer.echo(result.patch, nl=False)
        for warning in result.warnings:
            err_console.print(f"warning: {warning}", style="yellow", highlight=False)
    elif as_json:
        echo_json(result.model_dump(exclude_none=True))
    elif sys.stdout.isatty():
        console.print(Markdown(result.display_markdown))
    else:
        typer.echo(result.display_markdown)
    if not quiet and not as_json:
        err_console.print(
            f"run {result.run_id}  (fusion runs show {result.run_id})",
            style="dim",
            highlight=False,
            soft_wrap=True,
        )
    if diff_only and not result.patch:
        raise typer.Exit(EXIT_HALTED)


def register(app: typer.Typer) -> None:
    """Add the run commands to the root app."""
    app.command("ask")(ask)
    app.command("digest")(digest)
    app.command("patch")(patch)
    app.command("review-diff")(review_diff)
    app.command("debug")(debug)
    app.command("decide")(decide)
    app.command("plan")(plan)
    app.command("eval-answer")(eval_answer)
    app.command("compare-claude-runs")(compare_claude_runs)
