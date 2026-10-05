"""Where Fusion keeps user config, project config and its run database.

User-level state lives in platform directories (``platformdirs``), never in the working directory
of whatever process spawned Fusion (for example an MCP host's project).
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_path, user_data_path

APP_NAME = "fusion"
CONFIG_FILE = "config.yaml"
DB_FILE = "runs.db"
LEGACY_DB_FILE = "fusion_runs.db"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def user_config_dir() -> Path:
    """User config directory (``FUSION_CONFIG_DIR`` overrides the platform default)."""
    return _env_path("FUSION_CONFIG_DIR") or user_config_path(APP_NAME)


def user_data_dir() -> Path:
    """User data directory (``FUSION_DATA_DIR`` overrides the platform default)."""
    return _env_path("FUSION_DATA_DIR") or user_data_path(APP_NAME)


def user_config_file() -> Path:
    return user_config_dir() / CONFIG_FILE


def project_dir() -> Path:
    """Directory whose ``.fusion/config.yaml`` is the project layer (default: cwd)."""
    return _env_path("FUSION_PROJECT_DIR") or Path.cwd()


def project_config_file() -> Path:
    return project_dir() / ".fusion" / CONFIG_FILE


def default_db_path() -> Path:
    return user_data_dir() / DB_FILE


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Explicit argument, else ``FUSION_DB_PATH``, else the user data directory."""
    if db_path:
        return Path(db_path).expanduser()
    return _env_path("FUSION_DB_PATH") or default_db_path()


def legacy_db_path() -> Path:
    """Where v0.1.0 kept its database: a cwd-relative file."""
    return Path.cwd() / LEGACY_DB_FILE
