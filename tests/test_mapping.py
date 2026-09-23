from __future__ import annotations

from decimal import Decimal

from pmcore.venues.polymarket.normalize import parse_gamma_market
from strategies.ref_mm.mapping import resolution, sides, text


def test_boundary_keeps_unit_and_strictness() -> None:
    a = text.extract_boundary("Will unemployment be above 3%?")
    b = text.extract_boundary("Unemployment above 3")
    c = text.extract_boundary("Unemployment at least 3%")
    assert a is not None and a.lower == Decimal(3) and a.lower_strict and a.unit == "%"
    assert b is not None and b.unit == "" and a != b
    assert c is not None and not c.lower_strict and a != c
    r = text.extract_boundary("between 1.5% and 2.0%")
    assert r is not None and r.lower == Decimal("1.5") and r.upper == Decimal("2.0")
    assert text.extract_boundary("season 2025-26 champion") is None
    big = text.extract_boundary("less than 250,000 jobs")
    assert big is not None and big.upper == Decimal(250000) and big.upper_strict


def test_polarity_and_negation() -> None:
    assert text.polarity_inverted(
        "Will the game be played in Mexico?", "Game relocated away from Mexico?"
    )
    assert not text.polarity_inverted("Will X win?", "X wins?")
    assert text.negation_mismatch("Will X fail to pass?", "X passes?")


def test_scaffold_entities() -> None:
    assert text.scaffold_entities(["Will Alice win the primary?", "Will Bob win the primary?"]) == [
        "alice",
        "bob",
    ]


def _pm(outcomes: str, tokens: str = '["111","222"]', q: str = "Will X happen?") -> object:
    return parse_gamma_market(
        {
            "id": "1",
            "conditionId": "0xc",
            "question": q,
            "outcomes": outcomes,
            "clobTokenIds": tokens,
        }
    )


def test_sides_yes_no() -> None:
    m = _pm('["Yes","No"]')
    a = sides.align_sides(m, "X happens?", "Yes", "")  # type: ignore[arg-type]
    assert a.status == "RESOLVED" and a.reference_token_id == "111"
    m2 = _pm('["No","Yes"]')
    a2 = sides.align_sides(m2, "X happens?", "Yes", "")  # type: ignore[arg-type]
    assert a2.reference_token_id == "222"  # label, not index


def test_sides_named_outcomes_from_yes_sub_title() -> None:
    m = _pm('["Celtics","Lakers"]', q="Lakers vs Celtics")
    a = sides.align_sides(m, "Lakers vs Celtics Winner?", "Los Angeles Lakers", "")  # type: ignore[arg-type]
    assert (
        a.status == "RESOLVED" and a.reference_outcome == "Lakers" and a.reference_token_id == "222"
    )
    u = sides.align_sides(m, "Winner?", "Boston Bruins", "")  # type: ignore[arg-type]
    assert u.status == "UNRESOLVED"


def test_sides_reject_polarity_inversion() -> None:
    m = _pm('["Yes","No"]', q="Will the game be played in Mexico?")
    a = sides.align_sides(m, "Game relocated away from Mexico?", "Yes", "")  # type: ignore[arg-type]
    assert a.status == "REJECT"


def test_resolution_checks() -> None:
    k = {
        "title": "Unemployment above 3.0% in March 2026?",
        "yes_sub_title": "Yes",
        "rules_primary": "Resolves YES if the BLS reports ...",
        "scheduled_end": "2026-04-03T12:00:00Z",
    }
    r = resolution.evaluate(
        "Will US unemployment be above 3.0% in March 2026?",
        "Resolution source: Bureau of Labor Statistics",
        "2026-04-03T12:00:00Z",
        k,
    )
    names = {c.name: c.verdict for c in r.checks}
    assert (
        names["threshold"] == "PASS"
        and names["date_window"] == "PASS"
        and names["source"] == "PASS"
    )
    k2 = {**k, "title": "Unemployment above 3.5% in March 2026?"}
    r2 = resolution.evaluate(
        "Will US unemployment be above 3.0% in March 2026?", "", "2026-04-03T12:00:00Z", k2
    )
    assert r2.any_fail
    k3 = {**k, "title": "Unemployment above 3.0% in March 2027?"}
    assert resolution.evaluate(
        "Will US unemployment be above 3.0% in March 2026?", "", None, k3
    ).any_fail
