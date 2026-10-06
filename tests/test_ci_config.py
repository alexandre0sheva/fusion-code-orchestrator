"""The repository's supply-chain controls: pinned actions, dependency audit, Dependabot, coverage.

These read the workflow files, so a change that loosens a control fails here before it reaches CI.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
SHA = re.compile(r"^[0-9a-f]{40}$")
PACKAGES = ("orchestration", "providers", "bench")


def load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for job in workflow["jobs"].values() for s in job.get("steps", [])]


def test_there_is_a_workflow_to_check() -> None:
    assert WORKFLOWS, "no workflow files found under .github/workflows"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    """A tag can be moved to different code; a commit SHA cannot."""
    for step in steps(load(path)):
        uses = step.get("uses")
        if uses is None or uses.startswith("./"):
            continue
        name, _, ref = uses.partition("@")
        assert SHA.match(ref), f"{path.name}: '{uses}' is not pinned to a 40-character commit SHA"
        assert name.count("/") >= 1, uses


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_pinned_action_says_which_version_it_is(path: Path) -> None:
    for line in path.read_text().splitlines():
        if re.search(r"uses:\s+[\w./-]+@[0-9a-f]{40}", line):
            assert re.search(r"#\s*v\d", line), f"{path.name}: add the version as a comment: {line}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflows_default_to_read_only_permissions(path: Path) -> None:
    assert load(path).get("permissions") == {"contents": "read"}


def ci() -> dict[str, Any]:
    return load(ROOT / ".github" / "workflows" / "ci.yml")


def test_ci_audits_the_locked_dependencies() -> None:
    commands = " ".join(str(s.get("run", "")) for s in steps(ci()))
    assert "pip-audit" in commands
    assert "--locked" in commands  # audits what the lock file pins, not a fresh resolution


def test_the_audit_runs_on_a_schedule_as_well_as_on_changes() -> None:
    # A new advisory against an unchanged lock file should still turn something red.
    triggers = ci().get(True) or ci().get("on")  # YAML reads a bare `on` as the boolean True
    assert "schedule" in triggers


def test_ci_enforces_the_coverage_gate_on_the_core_packages() -> None:
    commands = "\n".join(str(s.get("run", "")) for s in steps(ci()))
    assert "--cov" in commands
    for package in PACKAGES:
        assert f"src/fusion/{package}" in commands, package
    assert commands.count("--fail-under=85") >= 1


def test_dependabot_watches_python_dependencies_and_actions() -> None:
    config = load(ROOT / ".github" / "dependabot.yml")
    assert config["version"] == 2
    ecosystems = {u["package-ecosystem"] for u in config["updates"]}
    assert {"github-actions", "uv"} <= ecosystems
    for update in config["updates"]:
        assert update["schedule"]["interval"] in {"daily", "weekly", "monthly"}


def test_the_coverage_gate_is_documented_where_contributors_look() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text()
    assert "85" in text and "coverage" in text.lower()


# ------------------------------------------------------------------------- packaging and release


def release() -> dict[str, Any]:
    return load(ROOT / ".github" / "workflows" / "release.yml")


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    found = workflow.get(True) or workflow.get("on")  # YAML reads a bare `on` as the boolean True
    assert isinstance(found, dict)
    return found


def test_ci_builds_the_package_and_smoke_tests_the_wheel() -> None:
    package = ci()["jobs"]["package"]
    text = "\n".join(str(s.get("run", "")) for s in package["steps"])
    assert "uv build" in text
    assert "evals/runners/check_wheel.py" in text
    assert "fusion version" in text and "--no-cache" in text  # a stale uv cache hides a bad wheel


def test_a_release_is_started_by_hand_and_only_by_hand() -> None:
    on = triggers(release())
    assert set(on) == {"workflow_dispatch"}
    assert "tag" in on["workflow_dispatch"]["inputs"]


def test_a_release_checks_the_package_before_it_publishes_anything() -> None:
    jobs = release()["jobs"]
    assert "build" in jobs and "github-release" in jobs
    assert jobs["github-release"]["needs"] == "build"
    build = "\n".join(str(s.get("run", "")) for s in jobs["build"]["steps"])
    assert "uv build" in build and "check_wheel.py" in build
    assert "fusion version" in build


def test_only_the_job_that_creates_the_release_may_write() -> None:
    for name, job in release()["jobs"].items():
        wanted = job.get("permissions", {})
        if name == "github-release":
            assert wanted == {"contents": "write"}
        else:
            assert "write" not in wanted.values(), name


def test_publishing_to_pypi_is_documented_but_not_switched_on() -> None:
    workflow = release()
    assert not any("pypi" in name.lower() for name in workflow["jobs"])
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "# " in text and "pypa/gh-action-pypi-publish" in text  # present, as a comment
    assert "id-token: write" in text and not any(
        "id-token" in job.get("permissions", {}) for job in workflow["jobs"].values()
    )


def test_publishing_later_is_explained_for_the_owner() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text()
    assert "## Publishing a release" in text
    assert "trusted publishing" in text.lower() and "release.yml" in text
