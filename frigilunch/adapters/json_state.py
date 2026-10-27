"""
json_state.py — rolling plan state persistence.

Atomic write via temp-file + rename: a crash during write leaves the old state
intact.  On Windows, Path.replace() is near-atomic (same-filesystem rename).

Schema version field 'schema_version' guards against reading a stale file.
Current version: 1.

read_state(path) → dict
write_state(path, state) → None
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class StateSchemaError(Exception):
    """Raised when the state file carries an unexpected schema version."""


def _validate_schema(raw: dict) -> None:
    """Reject state files from incompatible schema versions."""
    version = raw.get("schema_version")
    if version is None:
        raise StateSchemaError("Missing 'schema_version' field in state file")
    if version != SCHEMA_VERSION:
        raise StateSchemaError(
            f"State file has schema_version={version}, expected {SCHEMA_VERSION}"
        )


def read_state(path: Path) -> dict:
    """Read and validate the plan state file.

    Returns an empty baseline state when the file does not exist.
    Raises StateSchemaError for incompatible schema versions.
    """
    if not path.exists():
        return _empty_state()
    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate_schema(raw)
    return raw


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically write `state` to `path`.

    Writes to a .tmp sibling first, then renames it into place.
    A crash between the two operations leaves the old file untouched.
    """
    if "schema_version" not in state:
        state = {"schema_version": SCHEMA_VERSION, **state}

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX; near-atomic on Windows (same volume)


def _empty_state() -> dict:
    """Baseline state for a first-ever run."""
    return {
        "schema_version": SCHEMA_VERSION,
        "frozen_until": None,
        "committed_days": [],
        "completed_orders": [],
    }
