"""The prompt pack: Fusion's tunable prompts are data files, validated on load."""

from __future__ import annotations

from pathlib import Path

import pytest

from fusion.config.layers import ConfigError
from fusion.config.prompt_pack import (
    PACK_DIR,
    PACK_ENV,
    PROMPT_SPECS,
    PromptPack,
    active_pack,
    default_pack,
    load_prompt_pack,
    pack_errors,
    use_prompt_pack,
)
from fusion.orchestration import prompts
from fusion.orchestration.cache import request_key
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.strategy import load_strategy_book
from fusion.routing.classifier import TaskType


def test_every_prompt_is_a_markdown_file_in_the_package() -> None:
    files = {p.stem for p in PACK_DIR.glob("*.md")}
    assert files == set(PROMPT_SPECS)
    pack = load_prompt_pack(PACK_DIR)
    assert pack_errors(pack) == []
    assert {spec.axis for spec in PROMPT_SPECS.values()} == {"inv", "dom"}


def test_prompts_are_rendered_from_the_active_pack() -> None:
    base = default_pack()
    edited = base.with_edits({"role.debugging_hypothesis": "You debug by bisecting first."})
    with use_prompt_pack(edited):
        assert active_pack() is edited
        system = prompts.get_system_prompt(TaskType.DEBUGGING)
    assert system.startswith("You debug by bisecting first.\n")
    assert "Rules for your response" in system
    assert active_pack() is base
    assert "bisecting" not in prompts.get_system_prompt(TaskType.DEBUGGING)


def test_rules_keep_the_untrusted_rules_placeholder_filled() -> None:
    system = prompts.get_system_prompt(TaskType.CODE_REVIEW)
    assert "{{" not in system
    assert "untrusted" in system.lower()


def test_select_criteria_come_from_the_pack() -> None:
    edited = default_pack().with_edits(
        {"select": "Pick the candidate that names a test. Shape: {{shape}}."}
    )
    with use_prompt_pack(edited):
        text = prompts.build_select_prompt(
            task_type=TaskType.IMPLEMENTATION_PLAN, material="m", candidates=[("c1", "x")]
        )
    assert "Pick the candidate that names a test. Shape: the shape the task asks for" in text


def test_unknown_and_missing_placeholders_are_errors() -> None:
    base = default_pack()
    unknown = base.with_edits({"claims": "Use {{format}} please."})
    assert any("unknown placeholder {{format}}" in e for e in pack_errors(unknown))
    missing = base.with_edits({"rules": "Answer in JSON."})
    assert any("{{untrusted_rules}}" in e and "rules" in e for e in pack_errors(missing))
    empty = base.with_edits({"task.default": "   "})
    assert any("task.default" in e and "empty" in e for e in pack_errors(empty))


def test_an_override_directory_replaces_only_the_files_it_has(tmp_path: Path) -> None:
    (tmp_path / "task.default.md").write_text("You answer tersely.\n", encoding="utf-8")
    pack = load_prompt_pack(tmp_path)
    assert pack.text("task.default") == "You answer tersely."
    assert pack.text("claims") == default_pack().text("claims")
    assert pack.origin == str(tmp_path)


def test_an_override_directory_with_an_unknown_or_broken_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "judge_rules.md").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="judge_rules"):
        load_prompt_pack(tmp_path)
    (tmp_path / "judge_rules.md").unlink()
    (tmp_path / "rules.md").write_text("No placeholder here.", encoding="utf-8")
    with pytest.raises(ConfigError, match="untrusted_rules"):
        load_prompt_pack(tmp_path)


def test_the_environment_variable_selects_the_default_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "role.coding_reviewer.md").write_text("Review like a hawk.", encoding="utf-8")
    monkeypatch.setenv(PACK_ENV, str(tmp_path))
    default_pack.cache_clear()
    try:
        assert prompts.get_system_prompt(TaskType.CODE_REVIEW).startswith("Review like a hawk.")
    finally:
        monkeypatch.delenv(PACK_ENV)
        default_pack.cache_clear()


def test_digest_diff_and_write_round_trip(tmp_path: Path) -> None:
    base = default_pack()
    edited = base.with_edits({"self_check": base.text("self_check") + "\nCheck twice."})
    assert edited.digest != base.digest
    assert base.with_edits({}).digest == base.digest
    diff = base.diff(edited)
    assert "+++ b/self_check.md" in diff
    assert "+Check twice." in diff
    assert base.diff(base) == ""
    edited.write(tmp_path / "pack")
    assert load_prompt_pack(tmp_path / "pack").digest == edited.digest
    with pytest.raises(KeyError):
        base.with_edits({"nope": "x"})


def test_the_response_cache_key_depends_on_the_pack() -> None:
    ctx = PipelineContext(task_type=TaskType.DEFAULT, primary_content="q")
    strategy = load_strategy_book().get("solo-haiku")
    before = request_key(ctx, strategy)
    with use_prompt_pack(default_pack().with_edits({"task.default": "Be brief."})):
        assert request_key(ctx, strategy) != before
    assert request_key(ctx, strategy) == before


def test_pack_is_immutable() -> None:
    pack = default_pack()
    assert isinstance(pack, PromptPack)
    with pytest.raises(TypeError):
        pack.texts["claims"] = "x"  # type: ignore[index]
