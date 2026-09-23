"""Build the per-series Kalshi fee regime history (ASSUMPTIONS K-FEE-7).

Sources, in order of precedence for a given date:
1. Archived fee schedule PDFs (docs/refs/kalshi_fee_schedule_history/, from the Wayback
   Machine): the maker-fee series lists with their effective dates. Before the first list, no
   series charged maker fees. Taker multiplier 1 everywhere (the schedules list no exceptions
   for event contracts).
2. GET /series/fee_changes?show_historical=true (history from 2025-10-04): fee_type and
   fee_multiplier changes scheduled per series.
3. GET /series: the current fee_type and fee_multiplier of every series (used as the regime
   from the last known change onward, and as the only regime for series never listed).
4. GET /events/fee_changes: per-event overrides (fee_type_override, fee_multiplier_override),
   applied by the fee lookup when an event ticker is given.

Worked example: KXNBA. Before 2025-05-13: quadratic, maker 0. From 2025-05-13 (PDF list):
quadratic_with_maker_fees, multiplier 1, so M_maker = 1. If the API later records
fee_multiplier 0.5 from 2026-01-01, M_maker = 0.5 from then.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pmcore.data.checkpoint import checkpoint_dir, read_jsonl
from pmcore.fees.kalshi import FeeError, FeeRegime, FeeSchedule, maker_multiplier_from
from pmcore.venues.kalshi.normalize import parse_ts

# Maker-fee series lists transcribed from the archived schedules (docs/refs/kalshi_fee_schedule_history/*.txt).
PDF_MAKER_LISTS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "2025-05-13T00:00:00Z",
        "kalshi_fee_schedule_history/20250514231857.txt (Last Updated: May 13, 2025)",
        (
            "KXAAAGASM",
            "KXGDP",
            "KXPAYROLLS",
            "KXU3",
            "KXEGGS",
            "KXCPI",
            "KXCPIYOY",
            "KXFEDDECISION",
            "KXFED",
            "KXNBA",
            "KXNBAEAST",
            "KXNBAWEST",
            "KXNBASERIES",
            "KXNHL",
            "KXNHLEAST",
            "KXNHLWEST",
            "KXNHLSERIES",
            "KXINDY500",
            "KXPGA",
            "KXUSOPEN",
            "KXPGARYDER",
            "KXTHEOPEN",
            "KXPGASOLHEIM",
            "KXFOMENSINGLES",
            "KXFOWOMENSINGLES",
            "KXWMENSINGLES",
            "KXWWOMENSINGLES",
            "KXUSOMENSINGLES",
            "KXUSOWOMENSINGLES",
        ),
    ),
]
PDF_LIST_2025_09_PATH = "docs/refs/kalshi_fee_schedule_history/20250917164753.txt"


def _pdf_2025_09_series(root: Path) -> tuple[str, ...]:
    """Parse every 'Series tickers:' list from the 2025-09-17 schedule (effective 2025-09-18)."""
    p = root / PDF_LIST_2025_09_PATH
    if not p.exists():
        return ()
    import re

    t = re.sub(r"\s+", " ", p.read_text(encoding="utf-8"))
    out: list[str] = []
    for m in re.finditer(
        r"Series tickers?:\s*([A-Z0-9, ]+?)(?=\s*(?:The remaining|fees =|Maker|$))", t
    ):
        out.extend(x.strip() for x in m.group(1).split(",") if x.strip().startswith("KX"))
    return tuple(dict.fromkeys(out))


@dataclass(frozen=True, slots=True)
class EventOverride:
    event_ticker: str
    effective_ts: datetime
    fee_type: str | None
    fee_multiplier: Decimal | None


def build_schedule(
    root: Path | None = None, ck: Path | None = None
) -> tuple[FeeSchedule, dict[str, list[EventOverride]], dict[str, Any]]:
    """Return (schedule, event overrides by event ticker, build summary)."""
    from pmcore.data.holdout import repo_root

    root = root or repo_root()
    ck = ck or checkpoint_dir("kalshi")
    regimes: list[FeeRegime] = []
    summary: dict[str, Any] = {}
    series_rows = read_jsonl(ck / "series.jsonl")
    current = {s["ticker"]: s for s in series_rows}
    epoch = datetime(2021, 1, 1, tzinfo=UTC)
    # 1. Baseline: no maker fees, taker multiplier 1, for every known series.
    for tkr in current:
        regimes.append(
            FeeRegime(
                tkr,
                epoch,
                "quadratic",
                Decimal(1),
                Decimal(0),
                "baseline: no maker fees before the first PDF list",
            )
        )
    # 2. PDF lists.
    lists = list(PDF_MAKER_LISTS) + [
        (
            "2025-09-18T03:00:00Z",
            PDF_LIST_2025_09_PATH + " (effective 09/18/2025 3am ET)",
            _pdf_2025_09_series(root),
        )
    ]
    for eff, src, tickers in lists:
        ts = parse_ts(eff)
        for tkr in tickers:
            regimes.append(
                FeeRegime(tkr, ts, "quadratic_with_maker_fees", Decimal(1), Decimal(1), src)
            )
    summary["pdf_lists"] = [(eff, len(t)) for eff, _s, t in lists]
    # 3. API series fee changes.
    fc_path = ck / "series_fee_changes.json"
    n_api = 0
    if fc_path.exists():
        for ch in json.loads(fc_path.read_text()).get("series_fee_change_arr", []):
            try:
                regimes.append(
                    FeeRegime.from_api(
                        ch["series_ticker"],
                        parse_ts(ch["scheduled_ts"]),
                        ch["fee_type"],
                        ch["fee_multiplier"],
                        f"series_fee_changes {ch.get('id')}",
                    )
                )
                n_api += 1
            except FeeError:
                continue  # perps and flat schedules are not event-contract quadratic fees
    summary["api_series_changes"] = n_api
    # 4. Current values as the regime from "now" only when no later change exists is handled by
    #    FeeSchedule ordering: add the current value at the last-known change time if it differs.
    now = datetime.now(UTC)
    n_cur = 0
    for tkr, s in current.items():
        try:
            m = maker_multiplier_from(
                s.get("fee_type", "quadratic"), Decimal(str(s.get("fee_multiplier", 1)))
            )
        except FeeError:
            continue
        regimes.append(
            FeeRegime(
                tkr,
                now,
                s.get("fee_type", "quadratic"),
                Decimal(str(s.get("fee_multiplier", 1))),
                m,
                "GET /series current",
            )
        )
        n_cur += 1
    summary["series_current"] = n_cur
    schedule = FeeSchedule(regimes)
    overrides: dict[str, list[EventOverride]] = {}
    for o in read_jsonl(ck / "events_fee_changes.jsonl"):
        ov = EventOverride(
            o["event_ticker"],
            parse_ts(o["scheduled_ts"]),
            o.get("fee_type_override"),
            Decimal(str(o["fee_multiplier_override"]))
            if o.get("fee_multiplier_override") is not None
            else None,
        )
        overrides.setdefault(o["event_ticker"], []).append(ov)
    for lst in overrides.values():
        lst.sort(key=lambda x: x.effective_ts)
    summary["event_overrides"] = sum(len(v) for v in overrides.values())
    return schedule, overrides, summary


def maker_multiplier_at(
    schedule: FeeSchedule,
    overrides: dict[str, list[EventOverride]],
    series_ticker: str,
    event_ticker: str | None,
    ts: datetime,
) -> Decimal:
    regime = schedule.regime_at(series_ticker, ts)
    fee_type, mult = regime.fee_type, regime.taker_multiplier
    if event_ticker and event_ticker in overrides:
        for ov in overrides[event_ticker]:
            if ov.effective_ts <= ts:
                if ov.fee_type is not None:
                    fee_type = ov.fee_type
                if ov.fee_multiplier is not None:
                    mult = ov.fee_multiplier
    return maker_multiplier_from(fee_type, mult)
