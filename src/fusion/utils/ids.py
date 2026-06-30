"""ID generation utilities."""

from __future__ import annotations

import uuid


def new_run_id() -> str:
    """Generate a unique run identifier."""
    return f"run_{uuid.uuid4().hex[:16]}"
