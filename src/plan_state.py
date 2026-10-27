"""
plan_state.py — Rolling commit/lock state between planning runs.

Each run of create_plan.py commits (locks) the near-term slice of its own
output — the day schedules whose date falls within COMMIT_WEEKS of that run's
plan_start — into plan_state.json, and records how far that commit window
reaches as `frozen_until`. The next run reads both back: committed days are
replayed exactly as they were built, AND the solver is barred from placing
ANY order — new, previously rejected, whatever — on any day before
`frozen_until`, even if real idle capacity sits there. Freezing a week means
literally nothing about it can change, not just "already-committed orders
stay put"; an unused hour of capacity on a frozen day stays unused rather
than being offered to a newer/rescued order. Everything from `frozen_until`
onward stays provisional and gets freely (re-)solved each time.

The stored unit is the DAY, not the order. A committed date holds its two
line sequences in running order, each run's exact time-slot, and the warnings
the day builder raised. The day is a record, not a derivation.

That distinction is the whole point. The previous format stored loose orders
in a dict keyed by `identity@productie_datum@lijn` and regrouped them into
days on load, which let three separate leaks through — all three are now
impossible to represent rather than merely fixed:

  - Two chunks of one split order on the SAME day and line collided on that
    key, so one was silently dropped at commit time (77,984 units in one
    observed run). A run is now a position in a list; there is no key.
  - The commit read the solver's order list rather than the days actually
    built, so an order the day builder dropped was reported rejected AND
    committed, reappearing the next run on a frozen day. Only what sits in a
    DaySchedule's lijn1/lijn2 can be written now; `dropped` cannot reach it.
  - A day's warnings were not stored, so a replayed frozen day reported zero
    hard errors while the schedule the floor actually runs still had them.

Days whose date has already passed (< today) are "done" — they were actually
produced, so they're dropped entirely rather than carried forward or
re-planned.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path

from .models import Order

COMMIT_WEEKS = 4  # how many weeks (starting at this run's own plan_start) get locked for the next run
STATE_SCHEMA_VERSION = 2


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
    """
    One committed day, as built. `lijn1`/`lijn2` are in running order — that
    sequence is part of what was committed, not something to re-derive.
    """

    datum: date
    lijn1: list[LockedRun] = field(default_factory=list)
    lijn2: list[LockedRun] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def runs(self) -> list[LockedRun]:
        return self.lijn1 + self.lijn2


def legacy_order_identity(o: Order) -> str:
    """Identity written before the source plan number was available."""
    return f"{o.order_nr}#{o.art_nr}#{o.klant}#{o.lever_datum.isoformat()}"


def order_identity(o: Order) -> str:
    """
    Identity of the ORIGINAL (un-split) order — matches an order back to its
    entry in orders.json.

    Deliberately excludes `aantal`: the solver can split one order into
    several chunks on different days/lines, each carrying a partial `aantal`
    (see scheduler_ortools_v3.py's chunking). orders.json only ever has the
    original, whole-order `aantal` — a chunk's partial amount would never
    match anything on load, silently orphaning that chunk's lock every
    time. `lever_datum` (not `productie_datum`) is used since it's never
    overwritten by locking, so identity stays stable across runs even for
    an order that's currently locked.

    ``plan_nr`` is the unique business order-line key. The legacy composite
    remains a read alias so existing frozen plans migrate without moving runs.
    """
    if o.plan_nr:
        return f"plan:{o.plan_nr}"
    return legacy_order_identity(o)


def identity_aliases(o: Order) -> tuple[str, ...]:
    """All persisted identities that may refer to an order, newest first."""
    current = order_identity(o)
    legacy = legacy_order_identity(o)
    return (current,) if current == legacy else (current, legacy)


def _migrate_locked_to_days(locked: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """
    Convert a pre-day-format `locked` dict into `days`.

    The old format stored loose orders, so the day has to be rebuilt the same
    way the old loader did: group on productie_datum/lijn and order each line
    by its committed start time. `warnings` comes out empty — the old format
    never kept them. An entry with no stored render has no place in a
    sequence and cannot be replayed, so it is skipped and reported rather
    than guessed at; that order simply becomes free work again.
    """
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


def load_plan_state(path: str | Path) -> dict:
    """
    Load {"frozen_until": "YYYY-MM-DD" | None, "days": {...}, "warnings": [...]}.
    Empty/absent-file default: nothing frozen, nothing locked.

    Older files are migrated on read — both the bare {storage_key: entry}
    dict (no frozen_until) and the {"frozen_until", "locked"} shape that
    replaced it. `warnings` reports anything the migration could not carry
    over; it is transient (the caller prints it) and is never written back,
    since build_new_state always constructs a fresh state.
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
        "rejections": {},          # the old format never kept these
        "warnings": warnings,
    }


def roll_completed_orders(
    orders: list[Order],
    days_state: dict[str, dict],
    completed_state: dict[str, dict],
    today: date,
) -> dict[str, dict]:
    """Carry completed identities and add runs from newly elapsed days.

    Records remain until their delivery date has passed. This bridges the gap
    between a production day leaving the four-week day state and the source
    order leaving the active delivery book.
    """
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


#: Reason prefixes that mean this run never actually tried the order — its
#: whole window was already frozen, or already past. Such a run has nothing to
#: say about WHY the order cannot be made, so it must not overwrite the run
#: that does.
_NO_REAL_ATTEMPT = ("ARRIVED-AFTER-FREEZE", "WINDOW-PASSED")


def merge_rejection_history(
    previous: dict[str, dict], reasons: dict[str, str], week: int, keep: set[str]
) -> dict[str, dict]:
    """
    Carry forward WHY each order was rejected, across runs.

    Once the freeze boundary passes an order's delivery date it is reported as
    ARRIVED-AFTER-FREEZE forever, which says only that it is now unreachable —
    not what went wrong while it still had a chance. Measured on a real chain:
    all 10 such orders in a week-36 run had failed on capacity in the weeks
    before, some of them booked five months earlier. Without this the planner
    is told "ordered too late" about an order placed in April.

    `keep` bounds the file: identities still worth remembering (in the order
    book, delivery not yet passed). Everything else is dropped.
    """
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


# What follows this marker in a carried-forward reason is the ORIGINAL
# rejection reason, verbatim — including its own cause prefix. Shared so
# exceptions_report can read the real cause back out rather than re-deriving
# the wording, which would silently drift the day either side is reworded.
HISTORY_REASON_MARKER = "Laatste reden — "


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


def save_plan_state(state: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


def block_end(plan_start: date, frozen_until: str | None) -> date:
    """
    The first date THIS run's solver is allowed to place anything on.

    Everything before it was already frozen by an earlier run's commit
    window and must stay completely untouched — not just already-locked
    orders, but the days themselves: no new or recovered order may be
    inserted there even into genuinely idle capacity. `frozen_until` is
    carried over from the previous run's own `commit_window(...)[1]`; on a
    first-ever (or --reset) run there's nothing frozen yet, so solving can
    start right at `plan_start`.
    """
    if not frozen_until:
        return plan_start
    return max(plan_start, date.fromisoformat(frozen_until))


def partition_locked_days(
    orders: list[Order],
    days_state: dict[str, dict],
    today: date,
    completed_state: dict[str, dict] | None = None,
) -> tuple[list[LockedDay], list[Order], list[str]]:
    """
    Split `orders` into (locked_days, free) using a previously saved
    plan_state's `days` sub-dict.

    locked_days: every committed day still on or after `today`, with each run
      resolved back to its original order from `orders` (aantal/productie_datum/
      lijn overwritten from the stored run, so a split order's chunk carries
      just its own share) and its committed time-slot attached. Line lists keep
      the order they were stored in — that is the committed running sequence.
    free: every whole order with no run in a still-future committed day.
      Orders on a day that has already passed are "done": they were produced,
      so they appear in neither list.

    A whole order is excluded from `free` as soon as ANY run of it is locked
    — if a split order's chunks are still being solved for, its other
    not-yet-committed chunks won't be produced by a fresh solve here anyway
    (the whole order was already fully placed by the run that committed it),
    so leaving a same-identity leftover in `free` would just let the solver
    plan a redundant partial re-production of it.

    That last rule strands quantity when a split order straddles the commit
    boundary: the in-window chunk is frozen, the rest is neither locked nor
    free. Freezing the day is the correct half of that trade — it is the
    guarantee the whole system exists for — so the shortfall is reported per
    order instead of being absorbed silently. Forbidding the solver from
    splitting across `commit_end` removes the case entirely.

    Runs referencing an order no longer present in `orders` (e.g. cancelled)
    are skipped with a warning.
    """
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
            # Already produced — drop, don't carry forward, don't re-plan.
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


def in_commit_window(d: date, commit_start: date, commit_end: date) -> bool:
    """
    Is this production date inside the window [commit_start, commit_end)?

    ONE definition of the boundary. create_plan.py had three within forty lines
    — `< commit_end` when committing, `<= commit_end` when deciding which days
    to run the expensive optimal sequencer on, and `commit_end - 1 day` for the
    dashboard's committed band. An order landing exactly on commit_end got the
    costly render but was never committed, and the dashboard drew the band a
    day short of what the Excel treated as committed.

    Half-open is the correct reading: commit_end is the NEXT window's first
    day, which is also what `frozen_until` means to the following run.
    """
    return commit_start <= d < commit_end


def last_committed_day(commit_end: date) -> date:
    """The last day actually inside the window — for display only."""
    return commit_end - timedelta(days=1)


def commit_window(plan_start: date) -> tuple[date, date]:
    """
    The window of dates this run newly commits, as [commit_start, commit_end).

    Starts at this run's own plan_start and spans COMMIT_WEEKS weeks, e.g.
    running week 19 commits weeks 19-22. On the NEXT run (week 20, one week
    later), weeks 20-22 arrive already locked from this run (still-future
    days in plan_state.json — week 19 itself has aged into the past and is
    dropped as "done"), so only week 23 is newly free to commit — the commit
    window has effectively rolled forward by exactly one week.
    """
    commit_start = plan_start
    commit_end = commit_start + timedelta(weeks=COMMIT_WEEKS)
    return commit_start, commit_end


def _run_entry(po) -> dict:
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
    """
    Rolling next-state: every day schedule whose date falls in this run's own
    [commit_start, commit_end) window, stored exactly as it was built.

    `schedules` is the FINAL day plan — days replayed from the previous
    commit plus the ones newly derived — so a carried-forward day re-commits
    itself unchanged, and a still-locked day is never lost as long as it sits
    inside the window (which it always does: the previous run's commit
    reached one week less far than this run's does).

    Reading the built days rather than the solver's order list is what makes
    a phantom uncommittable. `schedule_all_days` drops anything that will not
    fit before shift end into DaySchedule.dropped; only lijn1/lijn2 are read
    here, so an order reported as a DAY-BUILD-DROP rejection cannot also be
    written into the frozen plan.
    """
    return {
        s.datum.isoformat(): {
            "lijn1": [_run_entry(po) for po in s.lijn1],
            "lijn2": [_run_entry(po) for po in s.lijn2],
            "warnings": list(s.warnings),
        }
        for s in schedules
        if in_commit_window(s.datum, commit_start, commit_end)
    }
