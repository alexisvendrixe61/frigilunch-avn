"""
models.py — core domain dataclasses.

Pure data: type hints, default values, no business logic.
Keep each class under 50 lines; use field() only for mutable defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Literal


@dataclass
class Product:
    """A product from the product master database."""

    art_nr: str
    naam: str
    schaal: str
    kapvorm: str
    snelheid: int                       # units per hour

    prio_l1: bool = False
    prio_l2: bool = False
    alleen_l1: bool = False
    alleen_l2: bool = False
    l1_uitgesloten: bool = False
    l2_uitgesloten: bool = False

    toegelaten_dagen: list[str] = field(default_factory=list)

    mep_d1: bool = False
    mep_dplus: bool = False

    allergie_niveau: int = 0
    allergenen: dict[str, str] = field(default_factory=dict)
    toestellen: dict[str, bool] = field(default_factory=dict)

    opstart_kan: bool = True
    afsluiten_kan: bool = True
    opstart_moet: bool = False
    opstart_kan_niet: bool = False
    afsluit_moet: bool = False
    afsluit_kan_niet: bool = False

    fte_base: float = 1.0
    personeel_assemblage_l1: int = 1
    personeel_assemblage_l2: int = 1

    identiek_groep: str | None = None  # G1: resolved group id

    mep_lead_days: int = 0
    maximum_per_dag: int = 0
    restricted_days: str = ""

    # Packaging — used by the Excel exporter
    stuks_per_karton: int = 0
    type_karton: str = ""
    # Raw assembly device counts, straight from the product database's
    # "Toestellen Assemblage" block — used by the "hulp mat." export column.
    assemblage_toestellen: dict = field(default_factory=dict)

    opmerking: str = ""
    # Provenance tags set by the loader (e.g. {"speed": "inferred"}).
    constraint_provenance: dict = field(default_factory=dict)


@dataclass
class Order:
    """A production order from the order book."""

    order_nr: str
    art_nr: str
    klant: str
    aantal: int
    lever_datum: date
    productie_datum: date
    lijn: int = 0           # 0 = unassigned; 1/2 = fixed by optimizer
    plan_nr: str = ""
    opmerking: str = ""
    opmerking_analyse: str = ""


@dataclass
class PlannedOrder:
    """An order placed on a specific line at a specific time."""

    order: Order
    product: Product
    lijn: int
    start: time
    eind: time
    ombouw_voor: int                    # changeover minutes before this order
    warnings: list[str] = field(default_factory=list)
    man: float = 1.0                    # FTE required


@dataclass
class DaySchedule:
    """The built timeline for one production day (both lines)."""

    datum: date
    lijn1: list[PlannedOrder] = field(default_factory=list)
    lijn2: list[PlannedOrder] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    kpis: dict = field(default_factory=dict)
    dropped: list[Order] = field(default_factory=list)
    positie_dropped: list[Order] = field(default_factory=list)
    device_dropped: list[tuple[Order, int, list[str]]] = field(default_factory=list)
    sate_dropped: list[tuple[Order, int]] = field(default_factory=list)


@dataclass(frozen=True, slots=True, order=True)
class PlanScore:
    """Lexicographic quality score — lower is better.

    Field order is business priority: completeness first,
    then setup efficiency, delivery proximity, forecast stability.
    """

    rejected_orders: int
    rejected_units: int
    transition_minutes: int
    scale_changes: int
    early_days: int
    moved_forecast_orders: int


@dataclass
class ValidationError:
    severity: str   # "ERROR" | "WARNING" | "INFO"
    code: str
    message: str
    order_nr: str = ""
    lijn: int = 0


@dataclass(frozen=True, slots=True)
class RejectionDiagnostic:
    order_key: str
    category: Literal[
        "late_after_freeze",
        "invalid_master_data",
        "proven_infeasible",
        "search_limit",
        "model_mismatch",
    ]
    attempted_slots: tuple[str, ...] = ()
    blocking_constraints: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SolveOutcome:
    """Result of one full solve (Benders loop to convergence)."""

    status: Literal["complete", "infeasible", "unknown"]
    scheduled: tuple[Order, ...]
    rejected: tuple[Order, ...]
    score: PlanScore
    diagnostics: tuple[RejectionDiagnostic, ...]
    stats: dict

    def __iter__(self):
        """Backward-compatible unpacking: scheduled, rejected, stats."""
        yield list(self.scheduled)
        yield list(self.rejected)
        yield self.stats
