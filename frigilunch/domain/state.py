"""
state.py — Pure functions for rolling-freeze state management.

No file I/O here — that lives in adapters/json_state.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from .models import DaySchedule, Order, PlannedOrder

COMMIT_WEEKS = 4

HISTORY_REASON_MARKER = "Laatste reden — "

_NO_REAL_ATTEMPT = ("ARRIVED-AFTER-FREEZE", "WINDOW-PASSED")


# ── dataclasses ───────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CompletedOrderRecord:
    """A produced order retained after its committed day ages out."""

    order_key: str
    plan_nr: str | None
    production_date: date
    delivery_date: date
    line: int
    units: int

    def to_dict(self) -> dict:
        return {
            "order_key": self.order_key,
            "plan_nr": self.plan_nr,
            "production_date": self.production_date.isoformat(),
            "delivery_date": self.delivery_date.isoformat(),
            "line": self.line,
            "units": self.units,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "CompletedOrderRecord":
        return cls(
            order_key=value["order_key"],
            plan_nr=value.get("plan_nr") or None,
            production_date=date.fromisoformat(value["production_date"]),
            delivery_date=date.fromisoformat(value["delivery_date"]),
            line=int(value["line"]),
            units=int(value["units"]),
        )


@dataclass
class LockedRun:
    """One committed production run: an order in the exact slot it was built into."""

    order: Order
    start: str
    eind: str
    ombouw_voor: int


@dataclass
class LockedDay:
    """One committed day, as built. lijn1/lijn2 are in running order."""

    datum: date
    lijn1: list[LockedRun] = field(default_factory=list)
    lijn2: list[LockedRun] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def runs(self) -> list[LockedRun]:
        return self.lijn1 + self.lijn2


# ── identity helpers ──────────────────────────────────────────────────────────

def legacy_order_identity(o: Order) -> str:
    """Identity written before the source plan number was available."""
    return f"{o.order_nr}#{o.art_nr}#{o.klant}#{o.lever_datum.isoformat()}"


def order_identity(o: Order) -> str:
    """Stable identity for an order (prefers plan_nr, falls back to composite)."""
    if o.plan_nr:
        return f"plan:{o.plan_nr}"
    return legacy_order_identity(o)


_order_key = order_identity  # internal alias


def identity_aliases(o: Order) -> tuple[str, ...]:
    """All persisted identities that may refer to an order, newest first."""
    current = order_identity(o)
    legacy = legacy_order_identity(o)
    return (current,) if current == legacy else (current, legacy)


def iso_weeks_before(year: int, week: int, n: int) -> list[tuple[int, int]]:
    """The n ISO (year, week) pairs immediately before this one, oldest first."""
    monday = date.fromisocalendar(year, week, 1)
    return [
        (monday - timedelta(weeks=k)).isocalendar()[:2]
        for k in range(n, 0, -1)
    ]


# ── planning window helpers ───────────────────────────────────────────────────

def exclusive_horizon_end(last_delivery: date) -> date:
    """First Monday strictly after the final delivery date."""
    end = last_delivery + timedelta(days=1)
    while end.weekday() != 0:
        end += timedelta(days=1)
    return end


def in_commit_window(d: date, commit_start: date, commit_end: date) -> bool:
    """Is this production date inside [commit_start, commit_end)?"""
    return commit_start <= d < commit_end


def last_committed_day(commit_end: date) -> date:
    """The last day actually inside the window — for display only."""
    return commit_end - timedelta(days=1)


def commit_window(plan_start: date) -> tuple[date, date]:
    """The window of dates this run newly commits, as [commit_start, commit_end)."""
    commit_start = plan_start
    commit_end = commit_start + timedelta(weeks=COMMIT_WEEKS)
    return commit_start, commit_end


def block_end(plan_start: date, frozen_until: str | None) -> date:
    """The first date THIS run's solver is allowed to place anything on."""
    if not frozen_until:
        return plan_start
    return max(plan_start, date.fromisoformat(frozen_until))


# ── state building ────────────────────────────────────────────────────────────

def _run_entry(po: PlannedOrder) -> dict:
    """One PlannedOrder as persisted — its identity, share, and exact slot."""
    return {
        "identity": order_identity(po.order),
        "aantal": po.order.aantal,
        "start": po.start.strftime("%H:%M"),
        "eind": po.eind.strftime("%H:%M"),
        "ombouw_voor": po.ombouw_voor,
    }


def build_new_state(
    schedules: list,
    commit_start: date,
    commit_end: date,
) -> dict[str, dict]:
    """Days in [commit_start, commit_end) stored exactly as built."""
    return {
        s.datum.isoformat(): {
            "lijn1": [_run_entry(po) for po in s.lijn1],
            "lijn2": [_run_entry(po) for po in s.lijn2],
            "warnings": list(s.warnings),
        }
        for s in schedules
        if in_commit_window(s.datum, commit_start, commit_end)
    }


def partition_locked_days(
    orders: list[Order],
    days_state: dict[str, dict],
    today: date,
    completed_state: dict[str, dict] | None = None,
) -> tuple[list[LockedDay], list[Order], list[str]]:
    """Split orders into (locked_days, free) using plan_state's days sub-dict."""
    by_identity = {alias: o for o in orders for alias in identity_aliases(o)}
    locked_days: list[LockedDay] = []
    warnings: list[str] = []
    locked_qty: dict[str, int] = {}
    done: set[str] = set(completed_state or {})
    locked_orders: set[str] = set()

    for iso in sorted(days_state):
        datum = date.fromisoformat(iso)
        stored = days_state[iso]
        if datum < today:
            for lijn in (1, 2):
                for entry in stored.get(f"lijn{lijn}", []):
                    done.add(entry["identity"])
            continue

        day = LockedDay(datum=datum, warnings=list(stored.get("warnings", [])))
        for lijn in (1, 2):
            for entry in stored.get(f"lijn{lijn}", []):
                order = by_identity.get(entry["identity"])
                if order is None:
                    warnings.append(
                        f"plan_state: order {entry['identity']} niet meer in orders.json — genegeerd"
                    )
                    continue
                run = LockedRun(
                    order=replace(
                        order, aantal=entry["aantal"], productie_datum=datum, lijn=lijn
                    ),
                    start=entry["start"],
                    eind=entry["eind"],
                    ombouw_voor=entry["ombouw_voor"],
                )
                (day.lijn1 if lijn == 1 else day.lijn2).append(run)
                locked_qty[entry["identity"]] = locked_qty.get(entry["identity"], 0) + entry["aantal"]
                locked_orders.add(order_identity(order))

        if day.runs:
            locked_days.append(day)

    for identity, qty in sorted(locked_qty.items()):
        whole = by_identity[identity].aantal
        if qty < whole:
            warnings.append(
                f"plan_state: order {identity} is maar deels vastgezet "
                f"({qty:,} van {whole:,} stuks) — de resterende {whole - qty:,} stuks "
                f"worden deze run NIET opnieuw ingepland (gesplitste order over de commit-grens)"
            )

    free = [
        o for o in orders
        if order_identity(o) not in locked_orders
        and not any(alias in done for alias in identity_aliases(o))
    ]
    return locked_days, free, warnings


def roll_completed_orders(
    orders: list[Order],
    days_state: dict[str, dict],
    completed_state: dict[str, dict],
    today: date,
) -> dict[str, dict]:
    """Carry completed identities and add runs from newly elapsed days."""
    by_identity = {alias: o for o in orders for alias in identity_aliases(o)}
    completed: dict[str, CompletedOrderRecord] = {}

    for identity, raw in completed_state.items():
        try:
            record = CompletedOrderRecord.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            continue
        if record.delivery_date >= today:
            order = by_identity.get(identity)
            canonical = order_identity(order) if order is not None else identity
            completed[canonical] = replace(record, order_key=canonical)

    for iso, stored in days_state.items():
        production_date = date.fromisoformat(iso)
        if production_date >= today:
            continue
        for line in (1, 2):
            for entry in stored.get(f"lijn{line}", []):
                identity = entry["identity"]
                order = by_identity.get(identity)
                if order is None or order.lever_datum < today:
                    continue
                canonical = order_identity(order)
                existing = completed.get(canonical)
                units = int(entry["aantal"])
                if existing is not None:
                    units += existing.units
                    production_date = min(production_date, existing.production_date)
                completed[canonical] = CompletedOrderRecord(
                    order_key=canonical,
                    plan_nr=order.plan_nr or None,
                    production_date=production_date,
                    delivery_date=order.lever_datum,
                    line=existing.line if existing is not None else line,
                    units=units,
                )

    return {key: record.to_dict() for key, record in sorted(completed.items())}


# ── rejection history ─────────────────────────────────────────────────────────

def merge_rejection_history(
    previous: dict[str, dict], reasons: dict[str, str], week: int, keep: set[str]
) -> dict[str, dict]:
    """Carry forward WHY each order was rejected, across runs."""
    history = {k: dict(v) for k, v in previous.items() if k in keep}
    for identity, reason in reasons.items():
        if identity not in keep or reason.startswith(_NO_REAL_ATTEMPT):
            continue
        entry = history.get(identity)
        if entry is None:
            history[identity] = {
                "eerste_week": week, "laatste_week": week, "pogingen": 1, "reason": reason,
            }
        else:
            entry["laatste_week"] = week
            entry["pogingen"] = entry.get("pogingen", 0) + 1
            entry["reason"] = reason
    return history


def explain_from_history(entry: dict | None) -> str:
    """The sentence appended to a frozen-out order's reason. Empty if unknown."""
    if not entry:
        return ""
    n = entry.get("pogingen", 0)
    first, last = entry.get("eerste_week"), entry.get("laatste_week")
    span = f"week {first}" if first == last else f"week {first} t/m {last}"
    return (
        f" WAAROM NIET GEPLAND: {n} eerdere run(s) ({span}) hebben hem geprobeerd "
        f"en het niet rond gekregen. {HISTORY_REASON_MARKER}{entry.get('reason', '')}"
    )


# ── publication policy ────────────────────────────────────────────────────────

def publication_blockers(outcome, phantom_count: int = 0) -> list[str]:
    """Reasons an optimizer result must not advance the frozen state."""
    blockers: list[str] = []
    if phantom_count:
        blockers.append(f"{phantom_count} assigned order(s) were dropped by final rendering")
    return blockers


def publication_notes(outcome) -> list[str]:
    """What would have blocked under the strict rule: printed, never blocking."""
    notes: list[str] = []
    if outcome.status == "unknown":
        notes.append("optimizer did not prove a complete or infeasible result")
    counts: dict[str, int] = {}
    for diagnostic in outcome.diagnostics:
        if diagnostic.category in {"invalid_master_data", "search_limit", "model_mismatch"}:
            counts[diagnostic.category] = counts.get(diagnostic.category, 0) + 1
    if counts:
        notes.append("rejections not proven impossible: " + ", ".join(
            f"{category} {n}" for category, n in sorted(counts.items())
        ))
    return notes


# ── legacy frozen_keys API (not used by pipeline, kept for backward compat) ──

def _week_monday(week: int, year: int) -> date:
    return date.fromisocalendar(year, week, 1)


def _serialize_order(order: Order) -> dict[str, Any]:
    return {
        "order_nr": order.order_nr,
        "art_nr": order.art_nr,
        "klant": order.klant,
        "aantal": order.aantal,
        "lever_datum": order.lever_datum.isoformat(),
        "productie_datum": order.productie_datum.isoformat(),
        "lijn": order.lijn,
        "plan_nr": order.plan_nr,
    }


def _deserialize_order(raw: dict[str, Any]) -> Order:
    return Order(
        order_nr=raw["order_nr"],
        art_nr=raw["art_nr"],
        klant=raw["klant"],
        aantal=raw["aantal"],
        lever_datum=date.fromisoformat(raw["lever_datum"]),
        productie_datum=date.fromisoformat(raw["productie_datum"]),
        lijn=raw.get("lijn", 0),
        plan_nr=raw.get("plan_nr", ""),
    )


def is_frozen(state: dict, order_key: str) -> bool:
    return order_key in state.get("frozen_keys", {})


def freeze_window(
    state: dict,
    week: int,
    year: int,
    schedule: list[DaySchedule],
) -> dict:
    monday = _week_monday(week, year)
    commit_end = monday + timedelta(weeks=COMMIT_WEEKS)
    frozen: dict[str, Any] = dict(state.get("frozen_keys", {}))
    for day in schedule:
        if not (monday <= day.datum < commit_end):
            continue
        for po in day.lijn1 + day.lijn2:
            key = _order_key(po.order)
            frozen[key] = _serialize_order(po.order)
    return {**state, "frozen_until": commit_end.isoformat(), "frozen_keys": frozen}


def load_frozen_orders(state: dict, current_week: int, year: int) -> list[Order]:
    monday = _week_monday(current_week, year)
    frozen_until_str = state.get("frozen_until")
    if not frozen_until_str:
        return []
    if date.fromisoformat(frozen_until_str) <= monday:
        return []
    orders: list[Order] = []
    for raw in state.get("frozen_keys", {}).values():
        order = _deserialize_order(raw)
        if order.productie_datum >= monday:
            orders.append(order)
    return orders
