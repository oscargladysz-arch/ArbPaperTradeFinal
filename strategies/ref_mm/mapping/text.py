"""Text helpers consolidated from the five legacy resolvers (one implementation, tested).

Differences from the legacy code, all root-cause fixes:
- numeric boundaries keep their unit (% or not), their strictness (strict vs inclusive), and
  are Decimal, so "above 3%" != "above 3" and "at least 5" != "more than 5";
- no hard-coded event tickers, no per-file stop-word drift;
- years are not stripped unless both sides carry the same year set.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

STOP_WORDS: frozenset[str] = frozenset(
    {
        "will",
        "the",
        "be",
        "in",
        "of",
        "for",
        "a",
        "an",
        "to",
        "on",
        "at",
        "by",
        "or",
        "and",
        "is",
        "this",
        "that",
        "who",
        "what",
        "which",
        "how",
        "next",
        "vs",
        "before",
        "after",
        "win",
        "winner",
        "won",
        "beat",
        "defeat",
        "game",
        "match",
        "yes",
        "no",
    }
)
_YEAR = re.compile(r"\b(20[2-3]\d)\b")
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE = re.compile(rf"\b({_MONTHS})\w*\.?\s+(\d{{1,2}})(?!\d)(?:,?\s+(20\d\d))?", re.IGNORECASE)


def normalize_text(text: str) -> str:
    t = str(text).lower().strip()
    t = t.replace("&", " and ")
    t = re.sub(r"[^\w\s%$.\-]", " ", t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def tokenize(text: str) -> list[str]:
    return [tok.strip(".-") for tok in normalize_text(text).split() if tok.strip(".-")]


def content_tokens(text: str) -> set[str]:
    return {t for t in tokenize(text) if t not in STOP_WORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def extract_years(text: str) -> set[int]:
    return {int(y) for y in _YEAR.findall(str(text))}


def extract_dates(text: str) -> set[tuple[str, int, int | None]]:
    out: set[tuple[str, int, int | None]] = set()
    for m in _DATE.finditer(str(text)):
        out.add((m.group(1)[:3].lower(), int(m.group(2)), int(m.group(3)) if m.group(3) else None))
    return out


def scaffold_entities(titles: list[str]) -> list[str]:
    """Remove tokens shared by every title in an event (the scaffolding) and stop words.

    ["Will Alice win?", "Will Bob win?"] -> ["alice", "bob"].
    """
    if not titles:
        return []
    toks = [tokenize(t) for t in titles]
    if len(titles) == 1:
        return [" ".join(t for t in toks[0] if t not in STOP_WORDS and not t.isdigit())]
    df = Counter()
    for ts in toks:
        df.update(set(ts))
    scaffolding = {t for t, c in df.items() if c >= len(titles)} | set(STOP_WORDS)
    return [" ".join(t for t in ts if t not in scaffolding) for ts in toks]


@dataclass(frozen=True, slots=True)
class Boundary:
    """A numeric condition: lower/upper bounds with strictness and unit.

    "above 3%"      -> lower=3, lower_strict=True, unit="%"
    "at least 3%"   -> lower=3, lower_strict=False, unit="%"
    "between 1.5% and 2.0%" -> lower=1.5, upper=2.0, both inclusive
    "less than 250,000" -> upper=250000, upper_strict=True, unit=""
    """

    lower: Decimal | None
    upper: Decimal | None
    lower_strict: bool
    upper_strict: bool
    unit: str


_NUM = r"\$?([\d][\d,]*\.?\d*)\s*(%|k|m|bn|b|million|billion|thousand)?"
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"between\s+{_NUM}\s+(?:and|to)\s+{_NUM}", re.I), "between"),
    (re.compile(rf"{_NUM}\s*(?:-|–|to)\s*{_NUM}", re.I), "range"),
    (
        re.compile(
            rf"(?:at least|or more|or higher|minimum of)\s*{_NUM}|{_NUM}\s*(?:or more|or higher|\+)",
            re.I,
        ),
        "ge",
    ),
    (
        re.compile(
            rf"(?:at most|or less|or lower|maximum of|no more than)\s*{_NUM}|{_NUM}\s*(?:or less|or lower|or fewer)",
            re.I,
        ),
        "le",
    ),
    (
        re.compile(
            rf"(?:above|more than|greater than|over|exceed(?:s|ing)?|higher than)\s*{_NUM}", re.I
        ),
        "gt",
    ),
    (re.compile(rf"(?:below|less than|under|fewer than|lower than)\s*{_NUM}", re.I), "lt"),
]
_MULT = {
    "k": Decimal(1000),
    "thousand": Decimal(1000),
    "m": Decimal(1_000_000),
    "million": Decimal(1_000_000),
    "b": Decimal(1_000_000_000),
    "bn": Decimal(1_000_000_000),
    "billion": Decimal(1_000_000_000),
}


def _num(raw: str | None, unit: str | None) -> tuple[Decimal | None, str]:
    if raw is None:
        return None, ""
    try:
        v = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None, ""
    u = (unit or "").lower()
    if u in _MULT:
        return v * _MULT[u], ""
    return v, "%" if u == "%" else ""


def extract_boundary(text: str) -> Boundary | None:
    t = str(text)
    for pat, kind in _PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        g = [x for x in m.groups()]
        if kind in ("between", "range"):
            lo, u1 = _num(g[0], g[1])
            hi, u2 = _num(g[2], g[3])
            if lo is None or hi is None:
                continue
            if kind == "range" and (lo > hi or (lo >= 1900 and hi >= 1900)):
                continue  # dates like 2025-26, not ranges
            return Boundary(lo, hi, False, False, u1 or u2)
        vals = [(g[i], g[i + 1]) for i in range(0, len(g), 2) if g[i] is not None]
        if not vals:
            continue
        v, u = _num(vals[0][0], vals[0][1])
        if v is None:
            continue
        if kind == "ge":
            return Boundary(v, None, False, False, u)
        if kind == "gt":
            return Boundary(v, None, True, False, u)
        if kind == "le":
            return Boundary(None, v, False, False, u)
        if kind == "lt":
            return Boundary(None, v, False, True, u)
    return None


INCLUSION_WORDS = (
    "played in",
    "held in",
    "hosted in",
    "hosted by",
    "takes place in",
    "remain",
    "stay",
    "kept",
    "keep",
    "join",
    "added",
    "approved",
    "confirmed",
    "enacted",
    "passed",
    "becomes",
    "elected",
    "nominated",
    "appointed",
    "launched",
    "released",
    "opens",
    "legalized",
    "allowed",
)
EXCLUSION_WORDS = (
    "relocated",
    "moved from",
    "moved out",
    "removed",
    "canceled",
    "cancelled",
    "exit",
    "leaves",
    "leaving",
    "departs",
    "departure",
    "rejected",
    "blocked",
    "vetoed",
    "struck down",
    "out as",
    "ousted",
    "fired",
    "resign",
    "banned",
    "outlawed",
    "prohibited",
    "withdraw",
)


def _has_word(text: str, words: tuple[str, ...]) -> bool:
    t = " " + normalize_text(text) + " "
    return any(re.search(rf"\b{re.escape(w)}\w*\b", t) for w in words)


def polarity_inverted(a: str, b: str) -> bool:
    """True when one text is inclusion-framed and the other exclusion-framed."""
    ai, ae = _has_word(a, INCLUSION_WORDS), _has_word(a, EXCLUSION_WORDS)
    bi, be = _has_word(b, INCLUSION_WORDS), _has_word(b, EXCLUSION_WORDS)
    return (ai and not ae and be and not bi) or (ae and not ai and bi and not be)


NEGATION = re.compile(r"\b(not|no longer|fail(?:s|ed)? to|never|won't|will not)\b", re.I)


def negation_mismatch(a: str, b: str) -> bool:
    return bool(NEGATION.search(a)) != bool(NEGATION.search(b))
