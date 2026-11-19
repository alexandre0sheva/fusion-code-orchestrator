"""The website (roadmap Task 18): the MkDocs config, the link-rewriting hook, the Pages workflow and
a real strict build. Offline; the build test needs the `site` dependency group and skips without it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SITE_URL = "https://alexandre0sheva.github.io/fusion-code-orchestrator/"
REPO = "https://github.com/alexandre0sheva/fusion-code-orchestrator"


def _load_hooks() -> ModuleType:
    path = ROOT / "scripts" / "mkdocs_hooks.py"
    spec = importlib.util.spec_from_file_location("mkdocs_hooks", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mkdocs_hooks"] = module
    spec.loader.exec_module(module)
    return module


hooks = _load_hooks()


def _repoint(markdown: str, page: str = "INTEGRATIONS.md", in_site: bool = True) -> str:
    page_dir = (DOCS / page).parent
    return str(hooks.repoint_links(markdown, page_dir, DOCS, ROOT, lambda _path: in_site))


def _mkdocs_config() -> dict[str, Any]:
    # `!!python/name` and `!!python/object` tags (the slug function) are MkDocs' business, not ours.
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("tag:yaml.org,2002:python/", lambda loader, suffix, node: None)
    data = yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=Loader)  # noqa: S506
    assert isinstance(data, dict)
    return data


def _nav_targets(nav: list[Any]) -> list[str]:
    targets: list[str] = []
    for item in nav:
        value = next(iter(item.values())) if isinstance(item, dict) else item
        if isinstance(value, list):
            targets += _nav_targets(value)
        else:
            targets.append(str(value))
    return targets


# --- the hook: links that leave docs/ -------------------------------------------------------


def test_a_link_that_leaves_docs_points_at_the_same_path_in_the_repository() -> None:
    out = _repoint("see [the model](../SECURITY.md#secret-redaction) and [readme](../README.md)")
    assert f"]({REPO}/blob/main/SECURITY.md#secret-redaction)" in out
    assert f"]({REPO}/blob/main/README.md)" in out
    assert "../" not in out


def test_a_link_to_a_directory_points_at_tree_and_keeps_the_title() -> None:
    out = _repoint('[data](../evals/datasets "the datasets")')
    assert out == f'[data]({REPO}/tree/main/evals/datasets "the datasets")'


def test_links_that_stay_on_the_site_are_left_alone() -> None:
    text = "[a](COSTS.md#price-tiers) [b](benchmark-report.html) [c](#top) [d](assets/x.png)"
    assert _repoint(text) == text


def test_external_and_absolute_links_are_left_alone() -> None:
    text = "[x](https://example.com/../a.md) [y](mailto:a@b.c) [z](/abs/path.md)"
    assert _repoint(text) == text


def test_a_link_that_points_nowhere_is_left_for_the_strict_build_to_report() -> None:
    text = "[gone](../NO_SUCH_FILE.md)"
    assert _repoint(text) == text


def test_a_link_into_a_page_excluded_from_the_site_points_at_the_repository() -> None:
    plan = "superpowers/plans/2026-11-17-v0.3.0-roadmap.md"
    assert (DOCS / plan).is_file()
    out = _repoint(f"[plan]({plan})", in_site=False)
    assert out == f"[plan]({REPO}/blob/main/docs/{plan})"
    assert _repoint(f"[plan]({plan})", in_site=True) == f"[plan]({plan})"


def test_a_link_out_of_the_repository_is_left_alone() -> None:
    text = "[up](../../outside.md)"
    assert _repoint(text) == text


# --- the config ------------------------------------------------------------------------------


def test_mkdocs_config_has_what_the_roadmap_asks_for() -> None:
    config = _mkdocs_config()
    assert config["site_url"] == SITE_URL
    assert config["repo_url"] == REPO
    assert config["docs_dir"] == "docs"
    assert config["strict"] is True
    assert "superpowers/" in config["exclude_docs"]
    assert "scripts/mkdocs_hooks.py" in config["hooks"]
    assert config["theme"]["name"] == "material"
    assert len(config["theme"]["palette"]) == 2  # light and dark
    assert {"navigation.sections", "content.code.copy"} <= set(config["theme"]["features"])
    extensions = [e if isinstance(e, str) else next(iter(e)) for e in config["markdown_extensions"]]
    assert "pymdownx.superfences" in extensions  # mermaid fences


def test_every_nav_entry_is_a_file_or_a_repository_link() -> None:
    targets = _nav_targets(_mkdocs_config()["nav"])
    assert "index.md" in targets and "benchmark-report.html" in targets
    for target in targets:
        if target.startswith("https://"):
            assert target.startswith(REPO + "/"), target
        else:
            assert (DOCS / target).is_file(), f"nav entry {target} is not in docs/"


def test_the_nav_covers_every_canonical_doc() -> None:
    targets = set(_nav_targets(_mkdocs_config()["nav"]))
    for name in ("INTEGRATIONS", "CONFIGURATION", "ARCHITECTURE", "COSTS", "BENCHMARKING",
                 "BENCHMARK_RESULTS", "CLAUDE_CODE_AB"):
        assert f"{name}.md" in targets


def test_the_landing_page_uses_files_that_exist() -> None:
    text = (DOCS / "index.md").read_text()
    needed_files = (
        "assets/benchmark-cost-quality.svg",
        "assets/dashboard.png",
        "benchmark-report.html",
    )
    for needed in needed_files:
        assert needed in text
        assert (DOCS / needed).is_file()
    assert "pip install" not in text  # not on PyPI (owner decision D3)


# --- the workflow ----------------------------------------------------------------------------


def _pages() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "pages.yml").read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML reads a bare `on` as True
    return data  # type: ignore[no-any-return]


def test_pages_workflow_triggers_and_never_deploys_a_pull_request() -> None:
    workflow = _pages()
    on = workflow["on"]
    assert on["push"]["branches"] == ["main"]
    paths = {"docs/**", "mkdocs.yml", "scripts/mkdocs_hooks.py", ".github/workflows/pages.yml"}
    assert paths <= set(on["push"]["paths"])
    assert "workflow_dispatch" in on and "pull_request" in on
    deploy = workflow["jobs"]["deploy"]
    assert deploy["if"] == "github.event_name != 'pull_request'"
    assert deploy["needs"] == "build"
    assert deploy["environment"]["name"] == "github-pages"
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert "permissions" not in workflow["jobs"]["build"]  # the build keeps read-only


def test_pages_build_is_strict_locked_and_uploads_the_site_directory() -> None:
    steps = _pages()["jobs"]["build"]["steps"]
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert "uv sync --locked" in runs and "group site" in runs
    assert "mkdocs build --strict" in runs
    upload = next(s for s in steps if "upload-pages-artifact" in s.get("uses", ""))
    assert upload["with"]["path"] == "site"
    assert upload["if"] == "github.event_name != 'pull_request'"
    assert "secrets." not in (ROOT / ".github" / "workflows" / "pages.yml").read_text()


def test_ci_runs_the_strict_build_so_a_broken_link_fails_the_pull_request() -> None:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    runs = "\n".join(str(s.get("run", "")) for s in ci["jobs"]["site"]["steps"])
    assert "mkdocs build --strict" in runs


def test_the_site_directory_is_not_committed() -> None:
    assert "site/" in (ROOT / ".gitignore").read_text().splitlines()


def test_the_site_dependency_group_is_declared() -> None:
    import tomllib

    groups = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]
    assert groups["site"] == ["mkdocs-material>=9.7,<10"]


# --- README and CONTRIBUTING ----------------------------------------------------------------


def test_readme_links_the_site_and_its_images_exist() -> None:
    import re

    readme = (ROOT / "README.md").read_text()
    assert SITE_URL in readme
    images = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", readme)
    assert {"docs/assets/architecture.svg", "docs/assets/demo.gif"} <= set(images)
    for image in (i for i in images if not i.startswith("https://")):
        assert (ROOT / image).is_file(), image
    assert (ROOT / "docs" / "assets" / "demo.tape").is_file()


def test_the_demo_tape_records_the_offline_provider_in_a_throwaway_repository() -> None:
    tape = (ROOT / "docs" / "assets" / "demo.tape").read_text()
    assert "Output docs/assets/demo.gif" in tape
    assert "--mock" in tape and "mktemp -d" in tape  # canned answers; never the user's repository


def test_contributing_documents_the_site_and_the_owner_step() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text()
    assert "## The website" in text
    assert "mkdocs build --strict" in text
    assert "Source" in text and "GitHub Actions" in text and "About" in text


# --- a real build ----------------------------------------------------------------------------


def test_the_strict_build_passes_and_rewrites_links(tmp_path: Path) -> None:
    pytest.importorskip("mkdocs")
    site = tmp_path / "site"
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "-d", str(site)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "NO_MKDOCS_2_WARNING": "1"},
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "WARNING" not in result.stderr
    assert (site / "index.html").is_file()
    assert (site / "benchmark-report.html").is_file()  # served as it is
    assert (site / "assets" / "dashboard.png").is_file()
    assert not (site / "superpowers").exists()  # plans are not product docs
    page = (site / "BENCHMARKING" / "index.html").read_text()
    assert f"{REPO}/blob/main/docs/superpowers/plans/" in page
    assert "../SECURITY.md" not in page
