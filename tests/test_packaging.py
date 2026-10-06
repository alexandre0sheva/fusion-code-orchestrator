"""What the wheel must carry, so `uvx --from <wheel or git URL> fusion ...` works without a clone.

Building a wheel needs the network (the build backend is fetched), so the real build is checked in
CI (the `package` job runs `evals/runners/check_wheel.py` on it). Here: the packaging
configuration, the checker itself on a synthetic wheel, and the dataset lookup a wheel relies on.
"""

from __future__ import annotations

import importlib.util
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from fusion.bench import spec

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "evals" / "runners" / "check_wheel.py"


def checker() -> ModuleType:
    found = importlib.util.spec_from_file_location("check_wheel", SCRIPT)
    assert found is not None and found.loader is not None
    module = importlib.util.module_from_spec(found)
    found.loader.exec_module(module)
    return module


def pyproject() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------- configuration


def test_the_wheel_force_includes_the_integrations_and_the_benchmark_dataset() -> None:
    wheel = pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]  # type: ignore[index]
    mapping = wheel["force-include"]  # type: ignore[index]
    assert mapping["integrations"] == "fusion/_integrations"
    assert mapping["evals/datasets/v1"] == "fusion/bench/datasets/v1"


def test_the_console_script_is_declared() -> None:
    scripts = pyproject()["project"]["scripts"]  # type: ignore[index]
    assert scripts["fusion"] == "fusion.cli.main:app"  # type: ignore[index]


# ----------------------------------------------------------------------------- dataset lookup


def test_a_packaged_dataset_is_found_by_name_and_so_are_its_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "site-packages" / "datasets"
    (package / "v1" / "coding" / "task-1").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)  # no ``evals`` here: this is not a clone
    monkeypatch.setattr(
        spec, "DATASET_DIRS", (package, package / "v1", *spec.DATASET_DIRS[1:]), raising=True
    )
    assert spec.resolve_dataset("v1") == package / "v1"
    assert spec.resolve_dataset("coding") == package / "v1" / "coding"


def test_the_lookup_order_ends_with_the_packaged_copy_before_the_repository_copies() -> None:
    package = Path(spec.__file__).parent / "datasets"
    assert spec.DATASET_DIRS[0] == package
    assert package / "v1" in spec.DATASET_DIRS
    assert spec.DATASET_DIRS.index(package / "v1") < spec.DATASET_DIRS.index(
        Path("evals") / "datasets" / "v1"
    )


# ---------------------------------------------------------------------------- the wheel checker


def make_repo(root: Path) -> None:
    (root / "src" / "fusion" / "config").mkdir(parents=True)
    (root / "src" / "fusion" / "__init__.py").write_text("")
    (root / "src" / "fusion" / "config" / "catalog.yaml").write_text("models: {}")
    (root / "src" / "fusion" / "__pycache__").mkdir()
    (root / "src" / "fusion" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (root / "integrations" / "cursor").mkdir(parents=True)
    (root / "integrations" / "cursor" / "mcp.json").write_text("{}")
    (root / "evals" / "datasets" / "v1").mkdir(parents=True)
    (root / "evals" / "datasets" / "v1" / "debugging.jsonl").write_text("{}\n")
    (root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')


GOOD = {
    "fusion/__init__.py": "",
    "fusion/config/catalog.yaml": "models: {}",
    "fusion/_integrations/cursor/mcp.json": "{}",
    "fusion/bench/datasets/v1/debugging.jsonl": "{}\n",
    "fusion_code_orchestrator-1.2.3.dist-info/METADATA": "Name: x\nVersion: 1.2.3\n",
    "fusion_code_orchestrator-1.2.3.dist-info/entry_points.txt": (
        "[console_scripts]\nfusion = fusion.cli.main:app\n"
    ),
}


def make_wheel(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


def test_a_complete_wheel_has_nothing_missing(tmp_path: Path) -> None:
    make_repo(tmp_path)
    wheel = make_wheel(tmp_path / "w.whl", GOOD)
    assert checker().problems(wheel, tmp_path) == []


@pytest.mark.parametrize(
    ("drop", "mention"),
    [
        ("fusion/config/catalog.yaml", "catalog.yaml"),
        ("fusion/_integrations/cursor/mcp.json", "_integrations"),
        ("fusion/bench/datasets/v1/debugging.jsonl", "debugging.jsonl"),
    ],
)
def test_a_file_the_wheel_lost_is_named(tmp_path: Path, drop: str, mention: str) -> None:
    make_repo(tmp_path)
    wheel = make_wheel(tmp_path / "w.whl", {k: v for k, v in GOOD.items() if k != drop})
    found = checker().problems(wheel, tmp_path)
    assert len(found) == 1 and mention in found[0]


def test_caches_and_bytecode_are_not_expected_in_the_wheel(tmp_path: Path) -> None:
    make_repo(tmp_path)
    wheel = make_wheel(tmp_path / "w.whl", GOOD)
    assert not any("pyc" in p for p in checker().problems(wheel, tmp_path))


def test_a_wheel_of_the_wrong_version_is_reported(tmp_path: Path) -> None:
    make_repo(tmp_path)
    stale = {**GOOD, "fusion_code_orchestrator-1.2.3.dist-info/METADATA": "Version: 1.2.2\n"}
    found = checker().problems(make_wheel(tmp_path / "w.whl", stale), tmp_path)
    assert any("1.2.2" in p and "1.2.3" in p for p in found)


def test_a_wheel_without_the_console_script_is_reported(tmp_path: Path) -> None:
    make_repo(tmp_path)
    without = {k: v for k, v in GOOD.items() if not k.endswith("entry_points.txt")}
    found = checker().problems(make_wheel(tmp_path / "w.whl", without), tmp_path)
    assert any("fusion" in p and "entry" in p.lower() for p in found)


def test_main_exits_nonzero_on_a_problem_and_zero_otherwise(tmp_path: Path) -> None:
    make_repo(tmp_path)
    good = make_wheel(tmp_path / "good.whl", GOOD)
    bad = make_wheel(tmp_path / "bad.whl", {"fusion/__init__.py": ""})
    mod = checker()
    assert mod.main([str(good), "--repo", str(tmp_path)]) == 0
    assert mod.main([str(bad), "--repo", str(tmp_path)]) == 1
