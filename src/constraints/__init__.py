"""
constraints/ — every production rule, one module per domain, indexed by
`registry.py`.

Start at `registry.py`: it lists every constraint the system knows about, its
severity, and the single point it is enforced at (FILTER / BUILD / VERIFY /
NONE). It is self-checking — `verify_registry()` resolves every implementation
path it claims — and it generates `docs/constraints.md`, so the documentation
cannot drift from the code.

| Module | Rules |
|---|---|
| `slots.py` | The FILTER point: every "may not run there" rule, in one function |
| `calendar_rules.py` | Weekday naming, per-line start times |
| `day_rules.py` | Allowed production weekdays, MEP lead time |
| `line_rules.py` | Line permission, exclusion, preference, load balancing |
| `allergen_rules.py` | Levels, drops, cross-contamination, sate exclusivity |
| `changeover.py` | Production duration, sequence-dependent setup time |
| `device_rules.py` | Shared single-unit machines |
| `grouping_rules.py` | Identical products, same kapvorm and same schaal together |
| `sequence_rules.py` | Order within a line-day, first/last-of-day rules |
| `staffing_rules.py` | Headcount ramp between orders |
"""
from .calendar_rules import (
    PRODUCTIEDAGEN,
    WEEKDAG_MAP,
    WERKDAGEN,
    get_earliest_start,
    weekdag,
)
from .allergen_rules import (
    SATE_EXCLUSIVE,
    SATE_NIVEAU,
    SATE_PARALLEL,
    allergie_daling,
    allergie_niveau_from_allergenen,
    check_kruiscontaminatie,
    check_sate_conflict,
    check_sate_sync,
    is_sate,
    overlaps_sate_window,
    sate_tail_index,
    sate_window,
)
from .changeover import (
    calculate_duration,
    calculate_setup_time,
    schaal_formaat,
    transition_cost,
)
from .day_rules import (
    allowed_days_for,
    check_dag_toegelaten,
    dag_toegelaten,
    get_mep_date,
    requires_restricted_days,
)
from .device_rules import check_toestel_conflict, is_scarce, scarce_devices_needed
from .grouping_rules import (
    identiek_groepen, kapvorm_sleutel, parse_identiek_refs, schaal_sleutel,
)
from .line_rules import (
    derive_line_info, line_permission_code, permitted_lines, preferred_line,
    resolve_lijn,
)
from .sequence_rules import (
    apply_positie_regels,
    mag_eerst,
    mag_laatst,
    moet_eerst,
    moet_laatst,
    positie_ok,
    sate_last,
    sluit_samen,
    sort_orders_for_lijn,
)
from .staffing_rules import check_bezetting_continuity, personeel_for_lijn

__all__ = [
    "PRODUCTIEDAGEN", "WEEKDAG_MAP", "WERKDAGEN", "get_earliest_start", "weekdag",
    "SATE_EXCLUSIVE", "SATE_NIVEAU", "SATE_PARALLEL", "allergie_daling", "allergie_niveau_from_allergenen",
    "check_kruiscontaminatie", "check_sate_conflict", "check_sate_sync", "is_sate",
    "overlaps_sate_window", "sate_tail_index",
    "sate_window",
    "calculate_duration", "calculate_setup_time", "schaal_formaat", "transition_cost",
    "allowed_days_for", "check_dag_toegelaten", "dag_toegelaten", "get_mep_date",
    "requires_restricted_days",
    "check_toestel_conflict", "is_scarce", "scarce_devices_needed",
    "identiek_groepen", "kapvorm_sleutel", "parse_identiek_refs", "schaal_sleutel",
    "derive_line_info", "line_permission_code", "permitted_lines", "preferred_line",
    "resolve_lijn",
    "apply_positie_regels", "mag_eerst", "mag_laatst", "moet_eerst", "moet_laatst",
    "positie_ok", "sate_last", "sluit_samen", "sort_orders_for_lijn",
    "check_bezetting_continuity", "personeel_for_lijn",
]
