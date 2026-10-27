from __future__ import annotations

from . import config
from .models import DaySchedule, PlannedOrder, ValidationError
from .constraints import (
    WEEKDAG_MAP,
    allergie_daling,
    check_dag_toegelaten,
    check_sate_conflict,
    check_sate_sync,
    check_toestel_conflict,
    get_mep_date,
    mag_eerst,
    mag_laatst,
    moet_eerst,
    moet_laatst,
    permitted_lines,
    preferred_line,
)


def validate_schedule(
    schedule: DaySchedule,
    products: dict,
    toestellen_beschikbaarheid: dict[str, int] | None = None,
) -> list[ValidationError]:
    """
    Voert alle checks uit op een DaySchedule.

    HARD ERRORS  -> planning moet aangepast worden
    WARNINGS     -> suboptimaal maar toegelaten
    INFO         -> ter informatie voor de planner
    """
    errors: list[ValidationError] = []
    weekdag = WEEKDAG_MAP.get(schedule.datum.weekday(), "")
    # Same source as schedule_day's own bound — these used to be two separate
    # hardcoded literals that could silently drift apart.
    max_end = config.shift_end(schedule.datum)

    for lijn_nr, planned in [(1, schedule.lijn1), (2, schedule.lijn2)]:
        for i, po in enumerate(planned):

            # ── HARD: lijn-toewijzing (L1, L2) ───────────────────────────────
            # Asks constraints.line_rules the same question the builder asked,
            # instead of re-deriving it from the four flags. These used to be
            # four inline checks here, which disagreed with permitted_lines on
            # a product excluded from BOTH lines.
            toegelaten = permitted_lines(po.product)
            if po.lijn not in toegelaten:
                errors.append(ValidationError(
                    severity="ERROR",
                    code="LIJN_ASSIGNMENT",
                    message=(
                        f"{po.product.naam} staat op L{po.lijn} maar mag enkel op "
                        f"{', '.join(f'L{n}' for n in toegelaten)}"
                    ),
                    order_nr=po.order.order_nr,
                    lijn=po.lijn,
                ))

            # ── INFO: lijn-voorkeur (L3) ─────────────────────────────────────
            # The soft half of the same question. INFO, not WARNING: running on
            # the other line is legal and is exactly what the rule asks for
            # once the preferred line is full ("producten prio L1 enkel op L2
            # inplannen indien geen plaats op L1"). It is reported so the
            # planner can see WHICH orders had to spill — and so the effect of
            # the solver's preference term stays measurable rather than
            # anecdotal. Neither the run summary's warning count nor the
            # Excel's count picks up an [INFO] line.
            voorkeur = preferred_line(po.product)
            if voorkeur is not None and po.lijn != voorkeur:
                errors.append(ValidationError(
                    severity="INFO",
                    code="LIJN_VOORKEUR",
                    # States the fact, not a cause. "L1 was full" is the
                    # USUAL reason and the one the solver's penalty leaves
                    # behind, but it is not the only one — a device conflict,
                    # a day-build drop, or a day frozen by an earlier run can
                    # put an order here too, and the validator cannot tell
                    # them apart from a schedule alone.
                    message=(
                        f"{po.product.naam} heeft voorkeur voor L{voorkeur} "
                        f"maar staat op L{po.lijn}"
                    ),
                    order_nr=po.order.order_nr,
                    lijn=po.lijn,
                ))

            # ── HARD: dag-restrictie ─────────────────────────────────────────
            dag_err = check_dag_toegelaten(po.product, schedule.datum)
            if dag_err:
                errors.append(ValidationError(
                    severity="ERROR",
                    code="DAG_RESTRICTIE",
                    message=dag_err,
                    order_nr=po.order.order_nr,
                    lijn=lijn_nr,
                ))

            # ── HARD: shift-eindtijd overschreden ────────────────────────────
            if po.eind > max_end:
                errors.append(ValidationError(
                    severity="ERROR",
                    code="SHIFT_OVERSCHRIJDING",
                    message=(
                        f"L{lijn_nr} order eindigt op {po.eind.strftime('%H:%M')}, "
                        f"max is {max_end.strftime('%H:%M')}"
                    ),
                    order_nr=po.order.order_nr,
                    lijn=lijn_nr,
                ))

            # ── HARD: negatieve ombouwtijd / overlap ─────────────────────────
            if po.ombouw_voor < 0:
                errors.append(ValidationError(
                    severity="ERROR",
                    code="NEGATIEVE_OMBOUW",
                    message=f"L{lijn_nr}: negatieve ombouwtijd {po.ombouw_voor} min",
                    order_nr=po.order.order_nr,
                    lijn=lijn_nr,
                ))

            # ── HARD: allergie-sequentie ─────────────────────────────────────
            if i > 0:
                prev = planned[i - 1]
                if allergie_daling(prev.product, po.product):
                    # Enkel ERROR als de scheduler GEEN reiniging had ingepland.
                    # De scheduler voegt "Allergie-daling" toe aan po.warnings wanneer
                    # hij reiniging heeft meegenomen in de ombouwtijd.
                    reiniging_ingepland = any(
                        "Allergie-daling" in w for w in po.warnings
                    )
                    if not reiniging_ingepland:
                        errors.append(ValidationError(
                            severity="ERROR",
                            code="ALLERGIE_DALING",
                            message=(
                                f"L{lijn_nr}: {prev.product.naam} niv.{prev.product.allergie_niveau} "
                                f"-> {po.product.naam} niv.{po.product.allergie_niveau} zonder reiniging"
                            ),
                            order_nr=po.order.order_nr,
                            lijn=lijn_nr,
                        ))

            # ── WARNING: kapvorm-wissel zonder allergie-reden ────────────────
            if i > 0:
                prev = planned[i - 1]
                if (
                    prev.product.kapvorm != po.product.kapvorm
                    and po.product.allergie_niveau == prev.product.allergie_niveau
                ):
                    errors.append(ValidationError(
                        severity="WARNING",
                        code="KAPVORM_WISSEL",
                        message=(
                            f"L{lijn_nr}: kapvorm-wissel zonder allergie-reden: "
                            f"{prev.product.kapvorm} -> {po.product.kapvorm}"
                        ),
                        order_nr=po.order.order_nr,
                        lijn=lijn_nr,
                    ))

            # ── WARNING: mep_dplus maar MEP datum te laat gepland ────────────
            if po.product.mep_dplus:
                mep_date = get_mep_date(po.order, po.product)
                if mep_date and mep_date >= schedule.datum:
                    errors.append(ValidationError(
                        severity="WARNING",
                        code="MEP_TE_LAAT",
                        message=(
                            f"MEP datum {mep_date.isoformat()} is niet vroeg genoeg "
                            f"(D-2 vereist, productie {schedule.datum.isoformat()})"
                        ),
                        order_nr=po.order.order_nr,
                        lijn=lijn_nr,
                    ))

            # ── INFO: allergie-niveau daalt binnen shift (met reiniging) ──────
            if i > 0:
                prev = planned[i - 1]
                if allergie_daling(prev.product, po.product) and po.ombouw_voor > 0:
                    errors.append(ValidationError(
                        severity="INFO",
                        code="ALLERGIE_DALING_MET_REINIGING",
                        message=(
                            f"L{lijn_nr}: allergie-daling niv.{prev.product.allergie_niveau}"
                            f"->{po.product.allergie_niveau} met {po.ombouw_voor} min reiniging"
                        ),
                        order_nr=po.order.order_nr,
                        lijn=lijn_nr,
                    ))

        # ── HARD: S2 opstart / afsluit ───────────────────────────────────────
        # Prohibitions and requirements are both errors since the planner's
        # 15/10 instruction ("set in stone"). The day builder guarantees both
        # (scheduler._enforce_positie_regels), so either firing is a bug.
        if planned:
            if not mag_eerst(planned[0].product):
                errors.append(ValidationError(
                    severity="ERROR",
                    code="OPSTART_VERBODEN",
                    message=(
                        f"L{lijn_nr}: {planned[0].product.naam} staat als eerste "
                        f"order van de dag maar mag niet opstarten"
                    ),
                    order_nr=planned[0].order.order_nr,
                    lijn=lijn_nr,
                ))
            if not mag_laatst(planned[-1].product):
                errors.append(ValidationError(
                    severity="ERROR",
                    code="AFSLUIT_VERBODEN",
                    message=(
                        f"L{lijn_nr}: {planned[-1].product.naam} staat als laatste "
                        f"order van de dag maar mag niet afsluiten"
                    ),
                    order_nr=planned[-1].order.order_nr,
                    lijn=lijn_nr,
                ))

            # A "moet" that was not honoured: the product is not in the run of
            # one article that opens (closes) the day. Two orders of the same
            # "moet" article open the day together, which is honoured.
            def _run(seq: list[PlannedOrder]) -> int:
                n = 1
                while n < len(seq) and seq[n].product.art_nr == seq[0].product.art_nr:
                    n += 1
                return n

            head_n, tail_n = _run(planned), _run(planned[::-1])
            for pos, label, wants, code, in_run in (
                (0, "opstarten", moet_eerst, "OPSTART_MOET_NIET_GEHONOREERD",
                 lambda i: i < head_n),
                (-1, "afsluiten", lambda p: moet_laatst(p) and not moet_eerst(p),
                 "AFSLUIT_MOET_NIET_GEHONOREERD",
                 lambda i: i >= len(planned) - tail_n),
            ):
                claimant = planned[pos]
                for i, po in enumerate(planned):
                    if not wants(po.product) or in_run(i):
                        continue
                    errors.append(ValidationError(
                        severity="ERROR",
                        code=code,
                        message=(
                            f"L{lijn_nr}: {po.product.naam} moet {label} maar staat "
                            f"op positie {i + 1} van {len(planned)} — "
                            f"{claimant.product.naam} heeft die plaats. Twee producten "
                            f"met dezelfde eis op één lijndag; verplaats er een naar "
                            f"een andere dag of pas de regel aan in Planning info.xlsx"
                        ),
                        order_nr=po.order.order_nr,
                        lijn=lijn_nr,
                    ))

    # ── HARD: sate op de ene lijn, productie op de andere (A5) ───────────────
    # The day builder slides the sate block to closing time and stops the other
    # line before it opens, so this should never fire — the same relationship
    # M1's staggering has with TOESTEL_CONFLICT below. It is here because
    # "should never" is not a guarantee anyone can eat.
    for conflict in check_sate_conflict(schedule.lijn1, schedule.lijn2):
        errors.append(ValidationError(
            severity="ERROR",
            code="SATE_CONFLICT",
            message=conflict,
            lijn=0,
        ))

    # ── WARNING: sate op beide lijnen start niet samen (A5, planner 22/10) ──
    # The builder opens both windows on the same minute; it only misses when a
    # slide would collide on a single-unit machine.
    niet_samen = check_sate_sync(schedule.lijn1, schedule.lijn2)
    if niet_samen:
        errors.append(ValidationError(
            severity="WARNING",
            code="SATE_NIET_GELIJKTIJDIG",
            message=niet_samen,
            lijn=0,
        ))

    # ── HARD: toestelconflicten ──────────────────────────────────────────────
    if toestellen_beschikbaarheid:
        conflicts = check_toestel_conflict(
            schedule.lijn1,
            schedule.lijn2,
            toestellen_beschikbaarheid,
        )
        for conflict in conflicts:
            errors.append(ValidationError(
                severity="ERROR",
                code="TOESTEL_CONFLICT",
                message=conflict,
                lijn=0,
            ))

    return errors
