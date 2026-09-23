"""Side alignment from labels and rules, never from token index (spec section 2, P-API-4).

Result: which Polymarket token is the pair's YES (the token that pays 1 when Kalshi YES pays 1)
and whether that was established deterministically. Anything ambiguous is UNRESOLVED and the
pair is rejected downstream. Heuristics never FLIP a side silently: an inversion detected by
polarity is a rejection reason, not a correction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pmcore.venues.polymarket.normalize import GammaMarket
from strategies.ref_mm.mapping.text import (
    content_tokens,
    jaccard,
    negation_mismatch,
    polarity_inverted,
)

Status = Literal["RESOLVED", "UNRESOLVED", "REJECT"]


@dataclass(frozen=True, slots=True)
class SideAlignment:
    status: Status
    reference_token_id: str | None
    reference_outcome: str | None
    method: str
    reason: str


def _is_yes_no(outcomes: tuple[str, ...]) -> bool:
    return tuple(o.strip().lower() for o in outcomes) in (("yes", "no"), ("no", "yes"))


def align_sides(
    poly: GammaMarket, kalshi_title: str, kalshi_yes_sub_title: str, kalshi_rules_primary: str
) -> SideAlignment:
    if len(poly.outcomes) != 2:
        return SideAlignment(
            "REJECT", None, None, "outcomes", f"{len(poly.outcomes)} outcomes, not binary"
        )
    if _is_yes_no(poly.outcomes):
        # Binary yes/no: YES token is the outcome labeled "Yes". Polarity of the questions
        # must agree; any inversion or negation mismatch is a rejection.
        if polarity_inverted(poly.question, kalshi_title) or polarity_inverted(
            poly.question, kalshi_rules_primary
        ):
            return SideAlignment(
                "REJECT",
                None,
                None,
                "yes_no",
                "inclusion/exclusion polarity inverted between questions",
            )
        if negation_mismatch(poly.question, kalshi_title):
            return SideAlignment(
                "REJECT", None, None, "yes_no", "negation present on one side only"
            )
        return SideAlignment(
            "RESOLVED",
            poly.token_for_outcome("Yes"),
            "Yes",
            "yes_no",
            "outcome labeled Yes is the reference token",
        )
    # Named outcomes (teams, candidates, brackets): match yes_sub_title against labels.
    ys = content_tokens(kalshi_yes_sub_title)
    if not ys:
        return SideAlignment("UNRESOLVED", None, None, "labels", "kalshi yes_sub_title empty")
    scores = []
    for o, t in zip(poly.outcomes, poly.token_ids, strict=True):
        ot = content_tokens(o)
        s = jaccard(ot, ys)
        subset = bool(ot) and (ot <= ys or ys <= ot)
        scores.append((max(s, 1.0 if subset else 0.0), o, t))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    if best[0] >= 0.5 and second[0] < 0.5 and best[0] > second[0]:
        return SideAlignment(
            "RESOLVED",
            best[2],
            best[1],
            "labels",
            f"yes_sub_title {kalshi_yes_sub_title!r} matches outcome {best[1]!r} (score {best[0]:.2f}), other {second[0]:.2f}",
        )
    return SideAlignment(
        "UNRESOLVED",
        None,
        None,
        "labels",
        f"yes_sub_title {kalshi_yes_sub_title!r} does not match exactly one outcome: {[(round(s, 2), o) for s, o, _ in scores]}",
    )
