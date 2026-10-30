"""
validator.py — Post-build sanity checks on a DaySchedule.

Ported from src.validator; all imports now point to frigilunch.
"""
from __future__ import annotations

from frigilunch.adapters import config
from frigilunch.domain.models import DaySchedule, PlannedOrder, ValidationError
from frigilunch.domain.constraints import (
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
    """Run all checks on a DaySchedule; return list of ValidationError."""
    errors: list[ValidationError] = []
    max_end = config.shift_end(schedule.datum)

    for lijn_nr, planned in [(1, schedule.lijn1), (2, schedule.lijn2)]:
        for i, po in enumerate(planned):

            # HARD: lijn-toewijzing
            toegelaten = permitted_lines(po.product)
            if po.lijn not in toegelaten:
                errors.append(ValidationError(
                    severity="ERROR", code="LIJN_ASSIGNMENT",
                    message=(
                        f"{po.product.naam} staat op L{po.lijn} maar mag enkel op "
                        f"{', '.join(f'L{n}' for n in toegelaten)}"
                    ),
                    order_nr=po.order.order_nr, lijn=po.lijn,
                ))

            # INFO: lijn-voorkeur
            voorkeur = preferred_line(po.product)
            if voorkeur is not None and po.lijn != voorkeur:
                errors.append(ValidationError(
                    severity="INFO", code="LIJN_VOORKEUR",
                    message=(
                        f"{po.product.naam} heeft voorkeur voor L{voorkeur} "
                        f"maar staat op L{po.lijn}"
                    ),
                    order_nr=po.order.order_nr, lijn=po.lijn,
                ))

            # HARD: dag-restrictie
            dag_err = check_dag_toegelaten(po.product, schedule.datum)
            if dag_err:
                errors.append(ValidationError(
                    severity="ERROR", code="DAG_RESTRICTIE",
                    message=dag_err,
                    order_nr=po.order.order_nr, lijn=lijn_nr,
                ))

            # HARD: shift-eindtijd
            if po.eind > max_end:
                errors.append(ValidationError(
                    severity="ERROR", code="SHIFT_OVERSCHRIJDING",
                    message=(
                        f"L{lijn_nr} order eindigt op {po.eind.strftime('%H:%M')}, "
                        f"max is {max_end.strftime('%H:%M')}"
                    ),
                    order_nr=po.order.order_nr, lijn=lijn_nr,
                ))

            # HARD: negatieve ombouwtijd
            if po.ombouw_voor < 0:
                errors.append(ValidationError(
                    severity="ERROR", code="NEGATIEVE_OMBOUW",
                    message=f"L{lijn_nr}: negatieve ombouwtijd {po.ombouw_voor} min",
                    order_nr=po.order.order_nr, lijn=lijn_nr,
                ))

            # HARD: allergie-sequentie
            if i > 0:
                prev = planned[i - 1]
                if allergie_daling(prev.product, po.product):
                    if not any("Allergie-daling" in w for w in po.warnings):
                        errors.append(ValidationError(
                            severity="ERROR", code="ALLERGIE_DALING",
                            message=(
                                f"L{lijn_nr}: {prev.product.naam} niv.{prev.product.allergie_niveau}"
                                f" -> {po.product.naam} niv.{po.product.allergie_niveau} zonder reiniging"
                            ),
                            order_nr=po.order.order_nr, lijn=lijn_nr,
                        ))

            # WARNING: kapvorm-wissel zonder allergie-reden
            if i > 0:
                prev = planned[i - 1]
                if (prev.product.kapvorm != po.product.kapvorm
                        and po.product.allergie_niveau == prev.product.allergie_niveau):
                    errors.append(ValidationError(
                        severity="WARNING", code="KAPVORM_WISSEL",
                        message=(
                            f"L{lijn_nr}: kapvorm-wissel zonder allergie-reden: "
                            f"{prev.product.kapvorm} -> {po.product.kapvorm}"
                        ),
                        order_nr=po.order.order_nr, lijn=lijn_nr,
                    ))

            # WARNING: mep_dplus maar MEP datum te laat
            if po.product.mep_dplus:
                mep_date = get_mep_date(po.order, po.product)
                if mep_date and mep_date >= schedule.datum:
                    errors.append(ValidationError(
                        severity="WARNING", code="MEP_TE_LAAT",
                        message=(
                            f"MEP datum {mep_date.isoformat()} is niet vroeg genoeg "
                            f"(D-2 vereist, productie {schedule.datum.isoformat()})"
                        ),
                        order_nr=po.order.order_nr, lijn=lijn_nr,
                    ))

            # INFO: allergie-daling mét reiniging
            if i > 0:
                prev = planned[i - 1]
                if allergie_daling(prev.product, po.product) and po.ombouw_voor > 0:
                    errors.append(ValidationError(
                        severity="INFO", code="ALLERGIE_DALING_MET_REINIGING",
                        message=(
                            f"L{lijn_nr}: allergie-daling niv.{prev.product.allergie_niveau}"
                            f"->{po.product.allergie_niveau} met {po.ombouw_voor} min reiniging"
                        ),
                        order_nr=po.order.order_nr, lijn=lijn_nr,
                    ))

        # HARD: S2 opstart / afsluit
        if planned:
            if not mag_eerst(planned[0].product):
                errors.append(ValidationError(
                    severity="ERROR", code="OPSTART_VERBODEN",
                    message=(
                        f"L{lijn_nr}: {planned[0].product.naam} staat als eerste "
                        f"order van de dag maar mag niet opstarten"
                    ),
                    order_nr=planned[0].order.order_nr, lijn=lijn_nr,
                ))
            if not mag_laatst(planned[-1].product):
                errors.append(ValidationError(
                    severity="ERROR", code="AFSLUIT_VERBODEN",
                    message=(
                        f"L{lijn_nr}: {planned[-1].product.naam} staat als laatste "
                        f"order van de dag maar mag niet afsluiten"
                    ),
                    order_nr=planned[-1].order.order_nr, lijn=lijn_nr,
                ))

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
                        severity="ERROR", code=code,
                        message=(
                            f"L{lijn_nr}: {po.product.naam} moet {label} maar staat "
                            f"op positie {i + 1} van {len(planned)} — "
                            f"{claimant.product.naam} heeft die plaats."
                        ),
                        order_nr=po.order.order_nr, lijn=lijn_nr,
                    ))

    # HARD: sate conflict across lines (A5)
    for conflict in check_sate_conflict(schedule.lijn1, schedule.lijn2):
        errors.append(ValidationError(
            severity="ERROR", code="SATE_CONFLICT", message=conflict, lijn=0,
        ))

    # WARNING: sate niet gelijktijdig (A5 parallel)
    niet_samen = check_sate_sync(schedule.lijn1, schedule.lijn2)
    if niet_samen:
        errors.append(ValidationError(
            severity="WARNING", code="SATE_NIET_GELIJKTIJDIG", message=niet_samen, lijn=0,
        ))

    # HARD: toestelconflicten (M1)
    if toestellen_beschikbaarheid:
        for conflict in check_toestel_conflict(
            schedule.lijn1, schedule.lijn2, toestellen_beschikbaarheid,
        ):
            errors.append(ValidationError(
                severity="ERROR", code="TOESTEL_CONFLICT", message=conflict, lijn=0,
            ))

    return errors
