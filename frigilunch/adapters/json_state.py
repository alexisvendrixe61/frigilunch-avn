"""
json_state.py — rolling plan state persistence.

Atomic write via temp-file + rename: a crash during write leaves the old state
intact. On Windows, Path.replace() is near-atomic (same-filesystem rename).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = 2

# ── migration helper ──────────────────────────────────────────────────────────

def _migrate_locked_to_days(locked: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """Convert a pre-day-format `locked` dict into `days`."""
    days: dict[str, dict] = {}
    warnings: list[str] = []

    for entry in locked.values():
        if "render" not in entry:
            warnings.append(
                f"plan_state: vastgezette order {entry.get('identity', '?')} heeft geen "
                f"opgeslagen rendering — kan niet worden teruggespeeld, wordt opnieuw ingepland"
            )
            continue
        day = days.setdefault(
            entry["productie_datum"], {"lijn1": [], "lijn2": [], "warnings": []}
        )
        day[f"lijn{entry['lijn']}"].append({
            "identity": entry["identity"],
            "aantal": entry["aantal"],
            "start": entry["render"]["start"],
            "eind": entry["render"]["eind"],
            "ombouw_voor": entry["render"]["ombouw_voor"],
        })

    for day in days.values():
        day["lijn1"].sort(key=lambda e: e["start"])
        day["lijn2"].sort(key=lambda e: e["start"])

    return days, warnings


# ── public API ────────────────────────────────────────────────────────────────

def load_plan_state(path: str | Path) -> dict:
    """
    Load plan state, migrating older formats transparently.

    Returns a dict with: schema_version, frozen_until, days, completed_orders,
    rejections, warnings. Empty/absent file returns a clean baseline.
    """
    path = Path(path)
    if not path.exists():
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "frozen_until": None,
            "days": {},
            "completed_orders": {},
            "rejections": {},
            "warnings": [],
        }
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)

    if "days" in state:
        state["schema_version"] = STATE_SCHEMA_VERSION
        state.setdefault("frozen_until", None)
        state.setdefault("completed_orders", {})
        state.setdefault("rejections", {})
        state.setdefault("warnings", [])
        return state

    # Back-compat: older files were a flat {storage_key: entry} dict with no
    # frozen_until, then {"frozen_until", "locked"} — both stored orders.
    locked = state.get("locked", state)
    days, warnings = _migrate_locked_to_days(locked)
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "frozen_until": state.get("frozen_until") if "locked" in state else None,
        "days": days,
        "completed_orders": {},
        "rejections": {},
        "warnings": warnings,
    }


def save_plan_state(state: dict, path: str | Path) -> None:
    """Atomically write state to path (temp file → rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


# ── legacy read_state / write_state (kept for backward compat) ───────────────

SCHEMA_VERSION = 1  # old version kept to avoid breaking callers


class StateSchemaError(Exception):
    """Raised when the state file carries an unexpected schema version."""


def _validate_schema(raw: dict) -> None:
    version = raw.get("schema_version")
    if version is None:
        raise StateSchemaError("Missing 'schema_version' field in state file")
    if version != SCHEMA_VERSION:
        raise StateSchemaError(
            f"State file has schema_version={version}, expected {SCHEMA_VERSION}"
        )


def read_state(path: Path) -> dict:
    """Read and validate state (old v1 format). Prefer load_plan_state for new code."""
    if not path.exists():
        return _empty_state()
    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate_schema(raw)
    return raw


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically write state (old v1 format). Prefer save_plan_state for new code."""
    if "schema_version" not in state:
        state = {"schema_version": SCHEMA_VERSION, **state}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _empty_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "frozen_until": None,
        "committed_days": [],
        "completed_orders": [],
    }
