"""Small JSON state files under the ops state directory (atomic replace, codes/counts/times only)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

MAX_STATE_BYTES = 1024 * 1024


def read(path: Path) -> dict | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("state_too_large")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("state_invalid")
    return value


def write(path: Path, value: dict) -> None:
    """Write a new file next to ``path`` and rename it over: a reader sees the old or the new state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
    with open(tmp, "xb") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=1).encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
