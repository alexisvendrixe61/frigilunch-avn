"""
exceptions_report.py — the orders a planner has to decide about by hand.

Most rejections are capacity: the plan is oversubscribed, and next week's run
may well place them. A few are not, and re-running will never help:

  ARRIVED-AFTER-FREEZE  The delivery window lies entirely inside days an
                        earlier run already committed. The frozen zone only
                        moves forward, so no future run can reach back for it.
  WINDOW-PASSED         The production window closed before this plan starts.
  LINE-DAY-RESTRICTION  No weekday/line this product may run on falls inside
                        its window at all.
  unknown product       The article is not in the product database.

Measured against the real order book, the freeze case is rare — 98.7% of
orders arrive more than four weeks before delivery, comfortably outside the
commit window, and only ~1% land inside it. That is why this is a short
exception list rather than a change to how freezing works: the mechanism is
right, a handful of orders just fall outside it and need a person.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from frigilunch.domain.constraints.changeover import calculate_duration
from frigilunch.domain.state import HISTORY_REASON_MARKER, order_identity

# Reason prefixes that no future solve can resolve, and what to do about each.
STANDING: dict[str, tuple[str, str]] = {
    # NOT "ordered too late": measured on a real run chain, all 10 orders in
    # this bucket were booked months ahead, tried and lost on capacity in
    # earlier runs, and only then had the freeze close over them. The `reason`
    # column carries that history — see plan_state.explain_from_history.
    "ARRIVED-AFTER-FREEZE": (
        "Leverdatum ligt volledig in een al bevroren week",
        "Zie de reden-kolom voor waarom hij eerder niet gepland raakte. "
        "Handmatig inplannen in het bevroren venster, leverdatum verschuiven, "
        "of overuren inplannen.",
    ),
    "WINDOW-PASSED": (
        "Productievenster al verstreken",
        "Achterstallig — buiten de planning om afhandelen.",
    ),
    "LINE-DAY-RESTRICTION": (
        "Geen toegelaten productiedag/lijn in het leveringsvenster",
        "Controleer de toegelaten dagen en lijn-instellingen in Planning info.xlsx.",
    ),
    "Product ": (
        "Artikel niet in de productdatabase",
        "Voeg het artikel toe aan Planning info.xlsx.",
    ),
    "MISSING-SPEED": (
        "Artikel heeft geen snelheid op lijn 1 of lijn 2",
        "Vul een geldige lijnsnelheid in Planning info.xlsx in; zonder snelheid kan "
        "de benodigde productietijd niet veilig worden berekend.",
    ),
    # Not "standing" in the same sense — a later run may well place it — but it
    # belongs on the planner's list anyway. The solver BELIEVED this order was
    # placed and the day builder then couldn't fit it, so it is the one class of
    # rejection nobody planned for. Letting it drain into the ordinary capacity
    # pile would repeat exactly the silence this cause was created to end.
    # Standing for the same reason ARRIVED-AFTER-FREEZE is: no later run can
    # fix it by itself. The product is barred from an end of the day and the
    # candidate days carry nobody else to take that end, so every future solve
    # reaches the identical dead end until a human changes something.
    "POSITIE-REGEL": (
        "Mag de dag niet openen/afsluiten, geen geschikte dagmaat op de toegewezen dag",
        "Vaak WEL plaatsbaar op een andere dag in het venster, samen met een order "
        "die de dag mag afsluiten — de solver zoekt daar alleen niet gericht naar. "
        "Koppel hem handmatig aan zo'n dag, pas de opstart/afsluit-regel van dit "
        "artikel aan in Planning info.xlsx, of verschuif de leverdatum.",
    ),
    "DAY-BUILD-DROP": (
        "Wel ingepland door de solver, niet in het dagschema geland",
        "Controleer de dag: de order paste niet meer voor sluitingstijd. "
        "Handmatig inplannen, of de leverdatum verschuiven.",
    ),
}


# Why the order was ACTUALLY lost, back when a run could still have placed it.
# A frozen-out order carries this inside its reason (plan_state.explain_from_
# history); without it the planner-facing columns describe the freeze — a
# consequence — and hand out a generic "expedite or add capacity" that is
# simply wrong when the real cause was, say, the position rule.
ONDERLIGGEND: dict[str, tuple[str, str]] = {
    "POSITIE-REGEL": (
        "mag de dag niet openen/afsluiten, geen geschikte dagmaat",
        "Koppel hem aan een order die de dag wel mag afsluiten — vaak kan dat "
        "op een andere dag in het venster — of pas de opstart/afsluit-regel van "
        "dit artikel aan in Planning info.xlsx.",
    ),
    "NEVER-PLACED": (
        "geen enkele dag met ruimte binnen het leveringsvenster",
        "Het venster was structureel overboekt. Verruim het venster of plan overuren.",
    ),
    "ORACLE-DROPPED": (
        "paste niet meer in het echte dagschema",
        "De dag liep vol op echte ombouwtijden. Handmatig herverdelen of overuren.",
    ),
    "COMBINED-SCHEDULE-DROP": (
        "geen ruimte naast de al vastgezette orders",
        "Handmatig herverdelen tegen het gecombineerde schema, of overuren.",
    ),
    "LINE-DAY-RESTRICTION": (
        "geen toegelaten productiedag/lijn in het venster",
        "Controleer de toegelaten dagen en lijn-instellingen in Planning info.xlsx.",
    ),
    "WINDOW-PASSED": (
        "productievenster was al verstreken",
        "Achterstallig — buiten de planning om afhandelen.",
    ),
    "Product ": (
        "artikel niet in de productdatabase",
        "Voeg het artikel toe aan Planning info.xlsx.",
    ),
    "MISSING-SPEED": (
        "artikel had geen snelheid op lijn 1 of lijn 2",
        "Vul een geldige lijnsnelheid in Planning info.xlsx in.",
    ),
}

# Appended to whatever the underlying cause asks for. The freeze does not
# change the CAUSE, it changes the DEADLINE for acting on it: no later run
# reaches back into a committed week, so this is the last chance to decide.
_BEVROREN_SUFFIX = (
    " Geen enkele volgende run pakt dit nog op (de week is bevroren) — "
    "beslis nu: inhalen in het bevroren venster of leverdatum verschuiven."
)


def _carried_cause(reason: str) -> tuple[str, str] | None:
    """The (label, actie) of the original failure carried inside `reason`."""
    _, marker, original = reason.partition(HISTORY_REASON_MARKER)
    if not marker:
        return None
    for prefix, pair in ONDERLIGGEND.items():
        if original.startswith(prefix):
            return pair
    return None


def _standing_kind(reason: str) -> str | None:
    for prefix in STANDING:
        if reason.startswith(prefix):
            return prefix
    return None


def diagnostic_category(reason: str) -> str:
    if reason.startswith("ARRIVED-AFTER-FREEZE"):
        return "late_after_freeze"
    if reason.startswith(("Product ", "MISSING-SPEED")):
        return "invalid_master_data"
    if reason.startswith(("WINDOW-PASSED", "LINE-DAY-RESTRICTION")):
        return "proven_infeasible"
    if reason.startswith(("ORACLE-DROPPED", "DAY-BUILD-DROP", "POSITIE-REGEL", "SATE")):
        return "model_mismatch"
    return "search_limit"


def build(rejected, products, rejection_reasons: dict[str, str]) -> list[dict]:
    """
    The rows a planner acts on: rejections that will still be rejected next
    week, and the week after, unless someone intervenes.

    Ordinary capacity rejections (NEVER-PLACED, ORACLE-DROPPED,
    COMBINED-SCHEDULE-DROP) are deliberately excluded — those are the solver's
    job and may resolve on their own as the order book shifts.
    """
    rows: list[dict] = []
    for o in rejected:
        reason = rejection_reasons.get(order_identity(o), "")
        kind = _standing_kind(reason)
        if kind is None:
            continue
        label, action = STANDING[kind]
        # A frozen-out order knows why it was really lost. Say that, and keep
        # the freeze as what it is: the reason it is now urgent, not the
        # reason it failed.
        if kind == "ARRIVED-AFTER-FREEZE":
            carried = _carried_cause(reason)
            if carried is not None:
                onderliggend, onderliggende_actie = carried
                label = f"Eerder niet ingepland ({onderliggend}), nu ook bevroren"
                action = onderliggende_actie + _BEVROREN_SUFFIX
        product = products.get(o.art_nr)
        rows.append({
            "order_nr": o.order_nr,
            "plan_nr": o.plan_nr,
            "order_date": o.order_nr,
            "art_nr": o.art_nr,
            "naam": product.naam if product else "",
            "klant": o.klant,
            "aantal": o.aantal,
            "lever_datum": o.lever_datum.isoformat(),
            "productie_minuten": calculate_duration(o, product) if product else None,
            "probleem": label,
            "actie": action,
            "reason": reason,
            "category": diagnostic_category(reason),
        })
    rows.sort(key=lambda r: (r["lever_datum"], r["art_nr"]))
    return rows


def print_report(rows: list[dict]) -> None:
    """Stdout summary. Silent when there is nothing to decide."""
    if not rows:
        return
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["probleem"], []).append(r)

    print(f"\n{'=' * 80}")
    print(f"[ACTIE VEREIST] {len(rows)} order(s) die GEEN enkele volgende run oplost")
    print(f"{'=' * 80}")
    for probleem, items in by_kind.items():
        units = sum(i["aantal"] for i in items)
        print(f"\n  {probleem} — {len(items)} order(s), {units:,} stuks")
        print(f"  -> {items[0]['actie']}")
        for r in items[:10]:
            print(f"     {r['lever_datum']}  {r['art_nr']:<12} {r['klant']:<14} "
                  f"{r['aantal']:>8,} st  {r['naam'][:38]}")
        if len(items) > 10:
            print(f"     ... en {len(items) - 10} meer")
    print()


def write_csv(rows: list[dict], path: str | Path) -> None:
    """A file the planner can open next to the Excel plan."""
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["lever_datum", "plan_nr", "art_nr", "naam", "klant", "aantal",
              "productie_minuten", "category", "probleem", "actie",
              "order_date", "order_nr", "reason"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
