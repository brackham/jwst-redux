"""Keep ephemeral MAST credentials out of persisted diagnostics."""

from __future__ import annotations

import os
from typing import Any


def redact(value: Any) -> Any:
    """Remove the active environment credential recursively, without retaining it."""
    if isinstance(value, str):
        token = os.environ.get("MAST_API_TOKEN")
        return value.replace(token, "[REDACTED]") if token else value
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
