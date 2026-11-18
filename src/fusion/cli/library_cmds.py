"""``fusion library ...``: the experience library's lessons, per project (``fusion.library``)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.table import Table

from fusion.cli.common import (
    CliError,
    DbPathOption,
    JsonOption,
    MockOption,
    QuietOption,
    console,
    echo_json,
    make_tools,
    run_tool,
)
from fusion.library.store import Lesson, LibraryStore, project_key
from fusion.mcp_server.response import present_feedback
from fusion.mcp_server.schemas import FeedbackInput
from fusion.mcp_server.tools import default_root

library_app = typer.Typer(
    help="Lessons Fusion learned from your feedback on runs, kept per project (opt-in: "
    "library.enabled)"
)

RootOption = Annotated[
    Path | None,
    typer.Option(
        "--root",
        help="The project whose lessons to use (default: FUSION_READ_ROOT, else the current "
        "directory)",
    ),
]


def _store(db_path: str | None) -> LibraryStore:
    return LibraryStore(db_path=db_path)


def _get(store: LibraryStore, lesson_id: str) -> Lesson:
    found = store.get(lesson_id)
    if found is None:
        msg = f"Lesson not found: {lesson_id}"
        raise CliError(msg, hint="List lessons with `fusion library list --all`.")
    return found


@library_app.command("list")
def library_list(
    root: RootOption = None,
    all_projects: Annotated[
        bool, typer.Option("--all", help="Every project's lessons, not just this one's")
    ] = False,
    as_json: JsonOption = False,
    db_path: DbPathOption = None,
) -> None:
    """List this project's lessons, newest first."""
    store = _store(db_path)
    key = None if all_projects else project_key(root or default_root())
    lessons = store.lessons(key)
    store.close()
    if as_json:
        echo_json([lesson.model_dump() for lesson in lessons])
        return
    if not lessons:
        where = "any project" if all_projects else "this project"
        console.print(f"No lessons for {where}.", highlight=False)
        return
    table = Table(title="Lessons")
    for column in ("ID", "Task", "When", "Do", "Verdict", "Runs"):
        table.add_column(column)
    for lesson in lessons:
        table.add_row(
            lesson.id,
            lesson.task_type,
            lesson.cue,
            lesson.lesson,
            f"{lesson.verdict} ({lesson.confidence})",
            str(len(lesson.evidence)),
        )
    console.print(table)


@library_app.command("show")
def library_show(
    lesson_id: Annotated[str, typer.Argument(help="Lesson id (from `fusion library list`)")],
    db_path: DbPathOption = None,
) -> None:
    """Show one lesson with its evidence and the runs whose prompt carried it (JSON)."""
    store = _store(db_path)
    try:
        lesson = _get(store, lesson_id)
        echo_json({**lesson.model_dump(), "used_in_runs": store.runs_using(lesson_id)})
    finally:
        store.close()


@library_app.command("forget")
def library_forget(
    lesson_id: Annotated[str | None, typer.Argument(help="Lesson id to delete")] = None,
    all_lessons: Annotated[
        bool, typer.Option("--all", help="Delete every lesson and feedback of this project")
    ] = False,
    root: RootOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask to confirm --all")] = False,
    db_path: DbPathOption = None,
) -> None:
    """Delete one lesson, or with --all every lesson and feedback record of this project."""
    if (lesson_id is None) == (not all_lessons):
        msg = "Name one lesson id, or pass --all (not both)"
        raise CliError(msg, hint="`fusion library forget L1234abcd` or `--all --root DIR`.")
    store = _store(db_path)
    try:
        if lesson_id is not None:
            _get(store, lesson_id)
            store.forget(lesson_id)
            console.print(f"Forgot {lesson_id}.", highlight=False)
            return
        key = project_key(root or default_root())
        count = len(store.lessons(key))
        if not yes:
            typer.confirm(f"Delete {count} lesson(s) and all feedback of this project?", abort=True)
        store.forget_project(key)
        console.print(f"Forgot {count} lesson(s).", highlight=False)
    finally:
        store.close()


@library_app.command("feedback")
def library_feedback(
    run_id: Annotated[str, typer.Argument(help="The run_id of a Fusion answer")],
    outcome: Annotated[
        Literal["used", "partly", "wrong", "ignored"],
        typer.Argument(help="used, partly, wrong or ignored"),
    ],
    note: Annotated[str, typer.Option("--note", help="Why, in a sentence or two")] = "",
    root: RootOption = None,
    mock: MockOption = False,
    as_json: JsonOption = False,
    quiet: QuietOption = False,
    db_path: DbPathOption = None,
) -> None:
    """Say what became of a run's answer (the fusion_feedback tool); may cost one cheap call."""
    tools = make_tools(db_path, mock)
    request = FeedbackInput(run_id=run_id, outcome=outcome, note=note)
    try:
        output = run_tool(tools, lambda t: t.fusion_feedback(request, root=root), quiet=quiet)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    result = present_feedback(output)
    if as_json:
        echo_json(result.model_dump(exclude_none=True))
    else:
        typer.echo(result.display_markdown)
