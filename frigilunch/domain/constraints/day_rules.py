"""day_rules.py — D1, D2. Which weekdays a product may run on, and MEP lead time."""
from __future__ import annotations

from datetime import date, timedelta

from ..models import Order, Product
from .calendar_rules import PRODUCTIEDAGEN, weekdag


def dag_toegelaten(product: Product, datum: date) -> bool:
    """D1 — may this product be produced on this weekday at all?"""
    return weekdag(datum) in product.toegelaten_dagen


def check_dag_toegelaten(product: Product, datum: date) -> str | None:
    """D1 — None if the day is allowed, an explanatory string if not."""
    if dag_toegelaten(product, datum):
        return None
    return (
        f"{product.naam} niet toegelaten op {weekdag(datum)} "
        f"(toegelaten: {', '.join(product.toegelaten_dagen)})"
    )


def get_mep_date(order: Order, product: Product) -> date | None:
    """D2 — when the mise-en-place for this order has to be ready."""
    if product.mep_dplus:
        return order.productie_datum - timedelta(days=2)
    if product.mep_d1:
        return order.productie_datum - timedelta(days=1)
    return None


def requires_restricted_days(naam: str, allergenen: dict[str, str]) -> bool:
    if "KIPPEBOUT" in naam.upper():
        return True
    return allergenen.get("vis", "-") in {"X", "+"}


def allowed_days_for(
    planning: dict,
    naam: str,
    allergenen: dict[str, str],
) -> list[str]:
    """D1 — which weekdays this product may be produced on.

    The spreadsheet's day columns win. With none set, chicken and fish fall
    back to the restricted di/wo/vr; everything else to all weekdays.
    """
    productiedagen = planning["productiedagen"]
    days = [
        day
        for day, key in [
            ("ma", "maandag_toegelaten"),
            ("di", "dinsdag_toegelaten"),
            ("wo", "woensdag_toegelaten"),
            ("do", "donderdag_toegelaten"),
            ("vr", "vrijdag_toegelaten"),
        ]
        if productiedagen[key]
    ]
    if days:
        return days
    if requires_restricted_days(naam, allergenen):
        return ["di", "wo", "vr"]
    return list(PRODUCTIEDAGEN)
