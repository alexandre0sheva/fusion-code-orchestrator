"""MkDocs hooks for the website: nothing in ``docs/`` is copied by hand, the build adapts instead.

- A Markdown link that leaves ``docs/`` (``../README.md``, ``../SECURITY.md#...``, ``../evals/...``)
  works on GitHub but would be a dead link on the site: it becomes a link to the same path in the
  repository. So does a link to a page ``exclude_docs`` keeps off the site (the plans).
- ``benchmark-report.html`` and ``assets/`` are served as they are (MkDocs copies every non-Markdown
  file of ``docs/``); the build fails if one of them goes missing, because the landing page and the
  navigation depend on them.

This module imports nothing from MkDocs at load time, so the tests can import it without the
``site`` dependency group.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "https://github.com/alexandre0sheva/fusion-code-orchestrator"
BRANCH = "main"
# Files of docs/ the site cannot do without (served as they are).
REQUIRED = ("benchmark-report.html", "assets/benchmark-cost-quality.svg", "assets/dashboard.png")

LINK = re.compile(r'(\]\()([^)\s#]*)(#[^)\s]*)?(\s+"[^"]*")?\)')
SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def repoint_links(
    markdown: str,
    page_dir: Path,
    docs: Path,
    root: Path,
    in_site: Callable[[Path], bool],
) -> str:
    """Rewrite links that do not stay on the site into links to the repository.

    ``page_dir`` is the directory of the page being built, ``docs`` the docs directory, ``root``
    the repository root and ``in_site(path)`` says whether a file inside ``docs`` is part of the
    site. A link that points nowhere is left alone so the strict build reports it.
    """

    def repoint(match: re.Match[str]) -> str:
        opening, target, fragment, title = match.groups()
        if not target or SCHEME.match(target) or target.startswith(("/", "//")):
            return match.group(0)
        resolved = (page_dir / unquote(target)).resolve()
        if not resolved.is_relative_to(root) or not resolved.exists():
            return match.group(0)
        if resolved.is_relative_to(docs) and in_site(resolved):
            return match.group(0)
        kind = "tree" if resolved.is_dir() else "blob"
        url = f"{REPOSITORY}/{kind}/{BRANCH}/{resolved.relative_to(root).as_posix()}"
        return f"{opening}{url}{fragment or ''}{title or ''})"

    return LINK.sub(repoint, markdown)


def on_files(files: Any, config: Any) -> Any:
    from mkdocs.exceptions import PluginError

    docs = Path(config["docs_dir"]).resolve()
    missing = [name for name in REQUIRED if not (docs / name).is_file()]
    if missing:
        raise PluginError(f"the site needs these files in docs/: {', '.join(missing)}")
    return files


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    docs = Path(config["docs_dir"]).resolve()

    def in_site(path: Path) -> bool:
        # An excluded file is still listed in `files`, marked as not included.
        file = files.get_file_from_path(path.relative_to(docs).as_posix())
        return file is not None and file.inclusion.is_included()

    return repoint_links(markdown, Path(page.file.abs_src_path).parent, docs, ROOT, in_site)
