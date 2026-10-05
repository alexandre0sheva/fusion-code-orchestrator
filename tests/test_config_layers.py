"""Config layering: packaged < user < project < env < CLI, with per-key provenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from fusion.config import loader as loader_module
from fusion.config.catalog import load_catalog
from fusion.config.layers import (
    ConfigError,
    deep_merge,
    resolve_config,
    set_cli_overrides,
)
from fusion.config.loader import load_baseline, load_routing_policies


def _write(path: Path, data: dict[str, Any] | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def user_file(fusion_home: Path) -> Path:
    return fusion_home / "config" / "config.yaml"


@pytest.fixture
def project_file(fusion_home: Path) -> Path:
    return fusion_home / "project" / ".fusion" / "config.yaml"


@pytest.fixture(autouse=True)
def _clear_cli_overrides() -> Any:
    set_cli_overrides({})
    yield
    set_cli_overrides({})


# ------------------------------------------------------------------------------------ merging


def test_deep_merge_merges_mappings_and_replaces_lists_and_scalars() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    merged = deep_merge(base, {"a": {"y": [9], "z": 3}, "b": {"c": 1}})
    assert merged == {"a": {"x": 1, "y": [9], "z": 3}, "b": {"c": 1}}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 1}  # inputs are never mutated


# --------------------------------------------------------------------- precedence matrix


def test_packaged_defaults_are_the_base_layer() -> None:
    resolved = resolve_config()
    assert resolved.data["fanout"]["max_concurrency"] == 6
    assert resolved.origins["fanout.max_concurrency"] == "packaged"
    assert "gpt-luna" in resolved.data["models"]


def test_user_overrides_packaged_and_project_overrides_user(
    user_file: Path, project_file: Path
) -> None:
    _write(user_file, {"fanout": {"max_concurrency": 4, "per_model_timeout_seconds": 30}})
    _write(project_file, {"fanout": {"max_concurrency": 2}})
    resolved = resolve_config()
    fanout = resolved.data["fanout"]
    assert fanout["max_concurrency"] == 2
    assert fanout["per_model_timeout_seconds"] == 30
    assert resolved.origins["fanout.max_concurrency"] == f"project:{project_file}"
    assert resolved.origins["fanout.per_model_timeout_seconds"] == f"user:{user_file}"
    assert resolved.origins["fanout.global_timeout_seconds"] == "packaged"


def test_env_overrides_files_and_cli_overrides_env(user_file: Path, project_file: Path) -> None:
    _write(user_file, {"fanout": {"max_concurrency": 4}})
    _write(project_file, {"fanout": {"max_concurrency": 3}})
    env = {"FUSION__FANOUT__MAX_CONCURRENCY": "2"}
    resolved = resolve_config(env=env)
    assert resolved.data["fanout"]["max_concurrency"] == 2
    assert resolved.origins["fanout.max_concurrency"] == "env:FUSION__FANOUT__MAX_CONCURRENCY"

    resolved = resolve_config(env=env, cli_overrides={"fanout.max_concurrency": 1})
    assert resolved.data["fanout"]["max_concurrency"] == 1
    assert resolved.origins["fanout.max_concurrency"] == "cli"


def test_process_wide_cli_overrides_are_applied() -> None:
    set_cli_overrides({"budgets.warn_cost_usd": 0.01})
    assert resolve_config().data["budgets"]["warn_cost_usd"] == 0.01


def test_env_values_are_parsed_as_yaml_scalars_and_lists() -> None:
    env = {
        "FUSION__FANOUT__ALLOW_PARTIAL_RESULTS": "false",
        "FUSION__BUDGETS__WARN_COST_USD": "0.25",
        "FUSION__REFINEMENT__ENABLED_BUDGETS": "[high]",
        "UNRELATED": "x",
    }
    data = resolve_config(env=env).data
    assert data["fanout"]["allow_partial_results"] is False
    assert data["budgets"]["warn_cost_usd"] == 0.25
    assert data["refinement"]["enabled_budgets"] == ["high"]


def test_models_merge_per_alias_so_a_user_can_flip_one_flag(user_file: Path) -> None:
    _write(user_file, {"models": {"ollama-llama": {"enabled": True}}})
    resolved = resolve_config()
    entry = resolved.data["models"]["ollama-llama"]
    assert entry["enabled"] is True
    assert entry["model_id"] == "llama3.2"  # untouched keys keep their packaged value
    assert resolved.origins["models.ollama-llama.enabled"] == f"user:{user_file}"


def test_empty_and_comment_only_files_are_fine(user_file: Path) -> None:
    _write(user_file, "# nothing here\n")
    assert resolve_config().data["fanout"]["max_concurrency"] == 6


def test_layers_report_which_files_were_read(user_file: Path, project_file: Path) -> None:
    _write(user_file, {"fanout": {"max_concurrency": 4}})
    names = [layer.name for layer in resolve_config().layers]
    assert names == ["packaged", f"user:{user_file}", "env", "cli"]
    _write(project_file, {"fanout": {"max_concurrency": 3}})
    names = [layer.name for layer in resolve_config().layers]
    assert names == ["packaged", f"user:{user_file}", f"project:{project_file}", "env", "cli"]


# ---------------------------------------------------------------------------- loader integration


def test_loaders_use_the_resolved_config(user_file: Path, project_file: Path) -> None:
    _write(
        user_file,
        {
            "provider_limits": {"openai": {"max_concurrent": 3}},
            "fanout": {"max_concurrency": 5},
            "baselines": [{"name": "Sol only", "model": "gpt-sol"}],
        },
    )
    _write(project_file, {"models": {"gpt-luna": {"max_tokens": 1234}}})
    assert load_catalog().provider_limits["openai"].max_concurrent == 3
    assert load_catalog().models["gpt-luna"].max_tokens == 1234
    assert load_routing_policies().fanout.max_concurrency == 5
    baselines = load_baseline().baselines
    assert [b.name for b in baselines] == ["Sol only"]
    assert baselines[0].model_id == "gpt-6.1-sol"


def test_explicit_paths_bypass_layering(user_file: Path) -> None:
    _write(user_file, {"fanout": {"max_concurrency": 5}})
    packaged = Path(loader_module.__file__).parent / "routing_policies.yaml"
    assert load_routing_policies(packaged).fanout.max_concurrency == 6


# -------------------------------------------------------------------------- human-readable errors


def test_invalid_value_error_names_file_key_and_hint(user_file: Path) -> None:
    _write(user_file, {"fanout": {"max_concurrency": 0}})
    with pytest.raises(ConfigError) as caught:
        load_routing_policies()
    message = str(caught.value)
    assert "fanout.max_concurrency" in message
    assert str(user_file) in message
    assert "greater than or equal to 1" in message


def test_env_origin_is_named_in_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    env_key = "FUSION__FANOUT__MAX_CONCURRENCY"
    monkeypatch.setenv(env_key, "-3")
    with pytest.raises(ConfigError) as caught:
        load_routing_policies()
    assert env_key in str(caught.value)


def test_unknown_top_level_key_suggests_the_closest_one(user_file: Path) -> None:
    _write(user_file, {"fanuot": {"max_concurrency": 2}})
    with pytest.raises(ConfigError) as caught:
        resolve_config()
    message = str(caught.value)
    assert "fanuot" in message
    assert "did you mean 'fanout'" in message
    assert str(user_file) in message


def test_yaml_syntax_error_names_file_and_line(user_file: Path) -> None:
    _write(user_file, "fanout:\n  max_concurrency: [1, 2\n")
    with pytest.raises(ConfigError) as caught:
        resolve_config()
    assert str(user_file) in str(caught.value)
    assert "line" in str(caught.value).lower()


def test_non_mapping_file_is_rejected(user_file: Path) -> None:
    _write(user_file, "- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        resolve_config()


def test_new_model_without_prices_gets_a_helpful_error(project_file: Path) -> None:
    _write(project_file, {"models": {"mine": {"provider": "openai", "model_id": "x"}}})
    with pytest.raises(ConfigError) as caught:
        load_catalog()
    assert "models.mine" in str(caught.value)
    assert "price" in str(caught.value).lower()
