"""Pure constraint rule functions — no I/O, no OR-Tools."""
from .allergen import (
    SATE_EXCLUSIVE, SATE_PARALLEL,
    allergen_level, allergie_daling, check_kruiscontaminatie,
    check_sate_conflict, check_sate_sync, is_sate,
    overlaps_sate_window, sate_tail_index, sate_window,
)
from .calendar_rules import (
    PRODUCTIEDAGEN, WEEKDAG_MAP, WERKDAGEN, get_earliest_start, weekdag,
)
from .changeover import calculate_duration, changeover_minutes, production_minutes, schaal_formaat, transition_cost
from . import slots
from .day_rules import (
    allowed_days_for, check_dag_toegelaten, dag_toegelaten,
    get_mep_date, requires_restricted_days,
)
from .device import (
    blocked_devices, busy_intervals, check_toestel_conflict,
    scarce_devices_needed, stagger_around,
)
from .grouping_rules import (
    GEEN_VOLGENDE, identiek_groepen, kapvorm_sleutel,
    parse_identiek_refs, schaal_sleutel, volgende_van,
)
from .line_rules import (
    derive_line_info, line_permission_code, permitted_lines,
    preferred_line, resolve_lijn,
)
from .sequencing import (
    apply_positie_regels, mag_eerst, mag_laatst, moet_eerst, moet_laatst,
    positie_ok, sate_last, sluit_samen, sort_orders_for_lijn,
)
from .staffing import check_bezetting_continuity, personeel_for_lijn
