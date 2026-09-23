"""Polymarket Gamma metadata client. Read-only (rule 10). ASSUMPTIONS P-API-1.."""

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

    def markets_page(self, **params: Any) -> list[dict[str, Any]]:
        data = self.c.get("/markets", params)
        return list(data) if isinstance(data, list) else []

    def closed_markets(
        self,
        *,
        page_limit: int = 500,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
        order: str = "id",
        ascending: bool = True,
    ) -> Iterator[list[dict[str, Any]]]:
        """Iterate closed markets in a date window.

        P-API-1 (OPEN): keyset pagination. Gamma documents `offset`; whether an id-keyset
        parameter exists must be verified. Until then this pages by `offset` inside narrow
        end-date windows, which the loader chooses small enough to stay under any offset cap,
        and it detects truncation by checking that the last page is short.
        """
        offset = 0
        while True:
            page = self.markets_page(
                closed="true",
                limit=page_limit,
                offset=offset,
                order=order,
                ascending=str(ascending).lower(),
                end_date_min=end_date_min,
                end_date_max=end_date_max,
            )
            yield page
            if len(page) < page_limit:
                return
            offset += page_limit
