"""Main entry points."""

from fusion.cli.app import app
from fusion.mcp_server.server import run_server

__all__ = ["app", "run_server"]

if __name__ == "__main__":
    app()
