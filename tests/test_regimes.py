from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pmcore.fees import regimes


def test_build_schedule_precedence(tmp_repo: Path) -> None:
    ck = tmp_repo / "data" / "checkpoints" / "kalshi"
    ck.mkdir(parents=True)
    (ck / "series.jsonl").write_text(
        json.dumps(
            {"ticker": "KXNBA", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
        )
        + "\n"
        + json.dumps({"ticker": "KXOTHER", "fee_type": "quadratic", "fee_multiplier": 1})
        + "\n"
    )
    (ck / "series_fee_changes.json").write_text(
        json.dumps(
            {
                "series_fee_change_arr": [
                    {
                        "id": "a",
                        "series_ticker": "KXNBA",
                        "fee_type": "quadratic_with_maker_fees",
                        "fee_multiplier": 0.5,
                        "scheduled_ts": "2026-01-01T00:00:00Z",
                    },
                    {
                        "id": "b",
                        "series_ticker": "KXPERP",
                        "fee_type": "margin_market_maker_program_fees",
                        "fee_multiplier": 0,
                        "scheduled_ts": "2026-01-01T00:00:00Z",
                    },
                ]
            }
        )
    )
    (ck / "events_fee_changes.jsonl").write_text(
        json.dumps(
            {
                "event_ticker": "KXOTHER-26X",
                "series_ticker": "KXOTHER",
                "fee_type_override": "quadratic_with_maker_fees",
                "fee_multiplier_override": 1,
                "scheduled_ts": "2026-03-01T00:00:00Z",
            }
        )
        + "\n"
    )
    sched, ov, summary = regimes.build_schedule(root=tmp_repo, ck=ck)
    u = lambda y, m, d: datetime(y, m, d, tzinfo=UTC)  # noqa: E731
    assert (
        regimes.maker_multiplier_at(sched, ov, "KXNBA", None, u(2025, 1, 1)) == 0
    )  # before the first PDF list
    assert (
        regimes.maker_multiplier_at(sched, ov, "KXNBA", None, u(2025, 6, 1)) == 1
    )  # PDF list of 2025-05-13
    assert regimes.maker_multiplier_at(sched, ov, "KXNBA", None, u(2026, 2, 1)) == Decimal(
        "0.5"
    )  # API change
    assert regimes.maker_multiplier_at(sched, ov, "KXOTHER", None, u(2026, 4, 1)) == 0
    assert (
        regimes.maker_multiplier_at(sched, ov, "KXOTHER", "KXOTHER-26X", u(2026, 4, 1)) == 1
    )  # event override
    assert summary["api_series_changes"] == 1
