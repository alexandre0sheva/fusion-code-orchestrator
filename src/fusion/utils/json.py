"""JSON serialization helpers."""

from __future__ import annotations

import json
from typing import Any


def safe_json_dumps(obj: Any, *, indent: int | None = None) -> str:
    """Serialize object to JSON, handling Pydantic models."""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, indent=indent, default=str)
