"""Deterministic identical-resolution checks (spec section 2): same underlying, threshold,
date window, resolution source, tie and cancellation handling. Each check returns PASS, FAIL,
or UNKNOWN; a FAIL rejects, an UNKNOWN sends the pair to the LLM pass, which is never the only
guard (rule 3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pmcore.venues.kalshi.normalize import parse_ts
from strategies.ref_mm.mapping.text import (
    content_tokens,
    extract_boundary,
    extract_dates,
    extract_years,
    jaccard,
)

Verdict = Literal["PASS", "FAIL", "UNKNOWN"]

SOURCE_PATTERNS = [
    r"associated press|\bap\b",
    r"\bbls\b|bureau of labor statistics",
    r"\bbea\b|bureau of economic analysis",
    r"\bcdc\b",
    r"\bnoaa\b|national weather service|\bnws\b",
    r"\bfed\b|federal reserve|fomc",
    r"\bsec\b",
    r"box office mojo",
    r"rotten tomatoes",
    r"billboard",
    r"spotify",
    r"nielsen",
    r"\bespn\b",
    r"\bnba\b|\bnfl\b|\bmlb\b|\bnhl\b",
    r"coingecko|coinmarketcap|binance|coinbase",
    r"\bcme\b",
    r"bloomberg",
    r"reuters",
    r"official (?:results?|website|announcement)",
    r"decision desk|\bddhq\b",
    r"new york times|nyt",
    r"polymarket",
    r"kalshi",
    r"\buma\b",
]

TIE_WORDS = re.compile(
    r"\b(tie|tied|draw|dead heat|no winner|postpone|cancel|abandon|void|refund|resolves? 50|split)\w*",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    verdict: Verdict
    detail: str


@dataclass(frozen=True, slots=True)
class ResolutionReport:
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @property
    def any_fail(self) -> bool:
        return any(c.verdict == "FAIL" for c in self.checks)

    @property
    def any_unknown(self) -> bool:
        return any(c.verdict == "UNKNOWN" for c in self.checks)

    def summary(self) -> str:
        return "; ".join(f"{c.name}={c.verdict}({c.detail})" for c in self.checks)


def check_threshold(poly_q: str, kalshi_title: str, kalshi_rules: str) -> Check:
    pb = extract_boundary(poly_q)
    kb = extract_boundary(kalshi_title) or extract_boundary(kalshi_rules)
    if pb is None and kb is None:
        return Check("threshold", "PASS", "no numeric threshold on either side")
    if pb is None or kb is None:
        return Check("threshold", "UNKNOWN", "numeric threshold on one side only")
    if pb == kb:
        return Check("threshold", "PASS", f"{pb.lower}..{pb.upper} {pb.unit}")
    return Check("threshold", "FAIL", f"poly {pb} vs kalshi {kb}")


def check_date_window(
    poly_q: str,
    poly_end: str | None,
    kalshi_title: str,
    kalshi_scheduled_end: str | None,
    tolerance: timedelta = timedelta(days=3),
) -> Check:
    """Compares the Polymarket scheduled endDate with the Kalshi SCHEDULED end
    (expected_expiration_time, fallback expiration_time). Never the actual close_time, which
    Kalshi moves earlier once an outcome is known (audit finding C1)."""
    py, ky = extract_years(poly_q), extract_years(kalshi_title)
    if py and ky and py != ky:
        return Check("date_window", "FAIL", f"years {sorted(py)} vs {sorted(ky)}")
    pd_, kd = extract_dates(poly_q), extract_dates(kalshi_title)
    if pd_ and kd and {(m, d) for m, d, _ in pd_} != {(m, d) for m, d, _ in kd}:
        return Check("date_window", "FAIL", f"dates {sorted(pd_)} vs {sorted(kd)}")
    if poly_end and kalshi_scheduled_end:
        try:
            pe = parse_ts(poly_end)
            ke = parse_ts(kalshi_scheduled_end)
        except ValueError:
            return Check("date_window", "UNKNOWN", "unparseable end dates")
        gap = abs(pe - ke)
        if gap > timedelta(days=45):
            return Check(
                "date_window",
                "FAIL",
                f"scheduled ends {gap.days} days apart (field: expected_expiration_time)",
            )
        if gap > tolerance:
            return Check(
                "date_window",
                "UNKNOWN",
                f"scheduled ends {gap.days} days apart (field: expected_expiration_time)",
            )
        return Check(
            "date_window", "PASS", f"scheduled ends within {gap} (field: expected_expiration_time)"
        )
    return Check("date_window", "UNKNOWN", "missing scheduled end date on one side")


def check_source(poly_desc: str, kalshi_rules: str) -> Check:
    ps = {p for p in SOURCE_PATTERNS if re.search(p, poly_desc, re.I)}
    ks = {p for p in SOURCE_PATTERNS if re.search(p, kalshi_rules, re.I)}
    ps.discard(r"polymarket")
    ps.discard(r"\buma\b")
    ks.discard(r"kalshi")
    if not ps or not ks:
        return Check("source", "UNKNOWN", "no recognizable source on one side")
    if ps & ks:
        return Check("source", "PASS", f"shared source patterns {len(ps & ks)}")
    return Check("source", "FAIL", f"poly sources {sorted(ps)} vs kalshi {sorted(ks)}")


def check_tie_handling(poly_desc: str, kalshi_rules: str) -> Check:
    pt = {m.group(1).lower()[:4] for m in TIE_WORDS.finditer(poly_desc)}
    kt = {m.group(1).lower()[:4] for m in TIE_WORDS.finditer(kalshi_rules)}
    if not pt and not kt:
        return Check("tie_handling", "PASS", "neither side mentions ties or cancellation")
    if pt == kt:
        return Check(
            "tie_handling", "UNKNOWN", f"both mention {sorted(pt)}; wording must be compared"
        )
    return Check("tie_handling", "UNKNOWN", f"poly mentions {sorted(pt)}, kalshi {sorted(kt)}")


def check_underlying(
    poly_q: str, kalshi_title: str, kalshi_yes_sub: str, min_jaccard: float = 0.34
) -> Check:
    pt = content_tokens(poly_q)
    kt = content_tokens(kalshi_title) | content_tokens(kalshi_yes_sub)
    j = jaccard(pt, kt)
    if j >= 0.6:
        return Check("underlying", "PASS", f"jaccard {j:.2f}")
    if j >= min_jaccard:
        return Check("underlying", "UNKNOWN", f"jaccard {j:.2f}")
    return Check("underlying", "FAIL", f"jaccard {j:.2f}")


def evaluate(
    poly_q: str, poly_desc: str, poly_end: str | None, k: dict[str, str | None]
) -> ResolutionReport:
    title = k.get("title") or ""
    rules = (k.get("rules_primary") or "") + " " + (k.get("rules_secondary") or "")
    return ResolutionReport(
        (
            check_underlying(poly_q, title, k.get("yes_sub_title") or ""),
            check_threshold(poly_q, title, rules),
            check_date_window(poly_q, poly_end, title, k.get("scheduled_end")),
            check_source(poly_desc, rules),
            check_tie_handling(poly_desc, rules),
        )
    )


def _dt(s: str | None) -> datetime | None:
    return parse_ts(s) if s else None
