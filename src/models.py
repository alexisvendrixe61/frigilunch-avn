from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Literal


@dataclass
class Product:
    art_nr: str
    naam: str
    schaal: str
    kapvorm: str
    type_karton: str
    stuks_per_karton: int
    snelheid: int

    prio_l1: bool
    prio_l2: bool
    alleen_l1: bool
    alleen_l2: bool

    toegelaten_dagen: list[str]

    mep_d1: bool
    mep_dplus: bool

    opstart_kan: bool
    afsluiten_kan: bool

    toestellen: dict[str, bool]
    allergenen: dict[str, str]
    allergie_niveau: int

    # Assembly devices as COUNTS, straight from the product database's
    # "Toestellen Assemblage" block. `toestellen` above flattens the same data
    # to booleans for the scheduler's scarce-device rules, which loses the
    # "2 Turbo's, not 1" the floor needs; the export's "hulp mat." column is
    # written from these counts.
    assemblage_toestellen: dict[str, int] = field(default_factory=dict)

    kliklok: bool = False
    etui: bool = False
    merk: str = ""
    opmerking: str = ""

    fte_base: float = 1.0              # FTE required for this product (0.5-2.0)
    skill_required: int = 1            # Skill level required (0-6)

    # Personeel (staff) requirements per task
    personeel_assemblage_l1: int = 1   # People needed for L1 assembly
    personeel_assemblage_l2: int = 1   # People needed for L2 assembly
    personeel_kartonnage: int = 1      # People needed for packaging
    personeel_keuken: int = 1          # People needed for kitchen

    kop: int = 0                       # Number of heads (kop)
    bovenplaten: int = 0               # Number of top plates (bovenplaten)

    # ── New Fields for Phase 2 Constraints ────────────────────────────────────

    # Line-specific (L2–L7)
    spiraal_l1: bool = True            # L2: Spiral (freezer) available on L1
    spiraal_l2: bool = True            # L2: Spiral available on L2

    # Production type (L6, L7, S4, S7)
    product_type: str = "other"        # 'rice', 'pasta', 'puree', 'other'
    weight_grams: float = 0.0          # L7: Product weight (800g on L2 has speed cap)

    # Sequencing (S1, S2)
    halal: bool = False                # S2: Halal production isolation
    opstart_moet: bool = False          # S1: Must be first of day
    opstart_kan_niet: bool = False      # S1: Cannot be first of day
    afsluit_moet: bool = False          # S1: Must be last of day
    afsluit_kan_niet: bool = False      # S1: Cannot be last of day

    # Order grouping (G1)
    identiek_product: str = ""          # G1: raw column-15 link list ("63319800 - 63963500")
    identiek_groep: str | None = None   # G1: resolved group id (smallest art_nr), see grouping_rules

    # Special rules (L4, D3, D4, D5, V2)
    maximum_per_dag: int = 0            # L4: Max productions per day (0 = no limit)
    restricted_days: str = ""           # D3: Special day restrictions (e.g., "do,vr")
    mep_lead_days: int = 0              # D5: Lead days for MEP >D-1 (parameterized)
    speed_cap: int | None = None        # V2: Production speed cap (units/hour)

    l1_uitgesloten: bool = False       # L1 expliciet 'kan_niet' — nooit op L1, ongeacht L2
    l2_uitgesloten: bool = False       # L2 expliciet 'kan_niet' — nooit op L2, ongeacht L1
    constraint_provenance: dict[str, str] = field(default_factory=dict)


@dataclass
class Order:
    order_nr: str
    art_nr: str
    klant: str
    aantal: int
    lever_datum: date
    productie_datum: date
    lijn: int = 0  # 0 = unassigned (schedule_day decides); 1/2 = fixed by optimizer

    # Per-order remarks from the "orderverwerking" tab, carried through to the
    # export unchanged. These belong to the ORDER, not the product: the same
    # article can ship with a different instruction each week ("halve batch op
    # vraag klant", "Folie perforeren, max 1000st/order"), so they cannot be
    # taken from the product database like Product.opmerking is.
    opmerking: str = ""            # column "opmerking"
    opmerking_analyse: str = ""    # column "opletten opmerking uit analyse"

    # The order's plan number ("plan nr." on the orderverwerking tab) — the
    # number the floor and the office both call this order by, unique over all
    # 1830 rows. It is NOT `order_nr`: that field holds the order's placement
    # DATE (see filter_by_known_date), which is what the point-in-time filter
    # and the plan_state identity are built on. This one is for reading, and
    # it is what the export's "nr" column shows.
    plan_nr: str = ""


@dataclass
class PlannedOrder:
    order: Order
    product: Product
    lijn: int
    start: time
    eind: time
    ombouw_voor: int
    warnings: list[str] = field(default_factory=list)
    man: float = 0.0  # FTE: number of people needed (from product.fte_base)


@dataclass
class DaySchedule:
    datum: date
    lijn1: list[PlannedOrder] = field(default_factory=list)
    lijn2: list[PlannedOrder] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    kpis: dict = field(default_factory=dict)
    # Orders handed to this day that the builder could not fit before shift
    # end. Structured, not just the [ERROR] CAPACITY_DROP warning text, so the
    # caller can move them into `rejected` instead of leaving them counted as
    # scheduled by a solver that never learned they were dropped.
    dropped: list[Order] = field(default_factory=list)
    # The subset of `dropped` removed to keep the day's ENDS legal (S2), not
    # because the day ran out of time. A different problem with a different
    # remedy: these orders fit on capacity and no amount of extra search or
    # splitting will place them — they need a day-mate allowed to take the end
    # they cannot. Kept separate so the rejection reason can say so instead of
    # reporting them as ordinary capacity drops.
    positie_dropped: list[Order] = field(default_factory=list)
    # The subset of `dropped` that fits this day perfectly well on its own and
    # was lost only because the OTHER line was holding a single-unit machine it
    # needs (M1). Carries the line it was on and the machines that blocked it.
    #
    # Also a different problem with a different remedy, and one the plan could
    # not previously tell apart from running out of time: the day builder knew
    # the reason at the moment it dropped the order and then discarded it, so
    # the solver learned "this set is too big" when the truth was "these two
    # cannot share a day". Kept separate so the feedback loop can cut the PAIR.
    # See docs/toestel-cut-gap.md.
    device_dropped: list[tuple[Order, int, list[str]]] = field(default_factory=list)
    # The subset of `dropped` that lost its place to A5: it fits the day and
    # needs no scarce machine, but it was still running when the other line
    # started its sate (peanut), and the hall has to be empty of everything
    # else for that. Carries the line it was on. A third distinct remedy —
    # neither more capacity nor a free machine helps, only a different day
    # from the one carrying the sate.
    sate_dropped: list[tuple[Order, int]] = field(default_factory=list)


@dataclass
class ValidationError:
    severity: str   # "ERROR" | "WARNING" | "INFO"
    code: str
    message: str
    order_nr: str = ""
    lijn: int = 0


@dataclass(frozen=True, slots=True, order=True)
class PlanScore:
    """One lexicographic quality definition for every optimizer stage.

    Lower is always better. Field order is business priority: completeness,
    exact setup efficiency, delivery proximity, then forecast stability.
    """

    rejected_orders: int
    rejected_units: int
    transition_minutes: int
    scale_changes: int
    early_days: int
    moved_forecast_orders: int


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
    status: Literal["complete", "infeasible", "unknown"]
    scheduled: tuple[Order, ...]
    rejected: tuple[Order, ...]
    score: PlanScore
    diagnostics: tuple[RejectionDiagnostic, ...]
    stats: dict

    def __iter__(self):
        """Backward-compatible unpacking for existing callers."""
        yield list(self.scheduled)
        yield list(self.rejected)
        yield self.stats
