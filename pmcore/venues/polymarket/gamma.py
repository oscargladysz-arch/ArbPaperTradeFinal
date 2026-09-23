"""Polymarket Gamma metadata client. Read-only (rule 10).

VERIFIED 2026-09-23 (docs/refs/gamma-openapi.yaml, live tests):
- GET /markets/keyset: limit max 100, after_cursor, closed, end_date_min, end_date_max (ISO),
  order, ascending, condition_ids, clob_token_ids. `offset` is rejected (422).
- GET /markets with offset returns 422 "offset too large, use /markets/keyset" near 9,500.
- Market fields include outcomes and clobTokenIds (JSON-encoded strings), description,
  resolutionSource, endDate, closedTime, umaResolutionStatus, gameStartTime,
  orderPriceMinTickSize, feeSchedule, restricted, events.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pmcore.venues.http import JsonClient


class Gamma:
    def __init__(self, base_url: str, *, ca_bundle: str | None = None, rps: float = 4.0) -> None:
        self.c = JsonClient(base_url, ca_bundle=ca_bundle, rps=rps)

    def close(self) -> None:
        self.c.close()

    def market(self, market_id: str) -> Any:
        return self.c.get(f"/markets/{market_id}")

    def event(self, event_id: str) -> Any:
        return self.c.get(f"/events/{event_id}")

    def closed_markets(
        self,
        *,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
        page_limit: int = 100,
        order: str = "id",
        ascending: bool = True,
    ) -> Iterator[list[dict[str, Any]]]:
        """Keyset-paginate closed markets whose endDate falls in [end_date_min, end_date_max]."""
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "closed": "true",
                "limit": page_limit,
                "order": order,
                "ascending": str(ascending).lower(),
                "end_date_min": end_date_min,
                "end_date_max": end_date_max,
            }
            if cursor:
                params["after_cursor"] = cursor
            data = self.c.get("/markets/keyset", params)
            items = list(
                data.get("data") or data.get("markets") or (data if isinstance(data, list) else [])
            )
            yield items
            pag = data.get("pagination") if isinstance(data, dict) else None
            cursor = (pag or {}).get("next_cursor") or (
                data.get("next_cursor") if isinstance(data, dict) else None
            )
            if not cursor or not items:
                return
