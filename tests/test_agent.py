"""Tests for workspace guard and agent helpers."""

from __future__ import annotations

import pytest

from fusion.agent.loop import _extract_action
from fusion.agent.workspace import WorkspaceError, WorkspaceGuard


def test_extract_action_from_json_block() -> None:
    text = 'Here is my action:\n```json\n{"action":"done","summary":"ok"}\n```'
    action = _extract_action(text)
    assert action is not None
    assert action["action"] == "done"


def test_workspace_blocks_escape(tmp_path) -> None:
    guard = WorkspaceGuard(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    resolved = guard.resolve("src/a.py")
    assert resolved.name == "a.py"
    with pytest.raises(WorkspaceError):
        guard.resolve("../../etc/passwd")


def test_workspace_resolve_dot(tmp_path) -> None:
    guard = WorkspaceGuard(tmp_path)
    assert guard.resolve(".") == guard.root
