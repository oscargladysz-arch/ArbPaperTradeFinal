"""Polymarket Data API trades client. Read-only. ASSUMPTIONS P-API-2, P-API-3.

Reported: offset cap of 10,000 and some time filters ignored. The loader measures both and
records the observed behavior; where truncated, on-chain fills become primary.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pmcore.venues.http import JsonClient


class DataApi:
    def __init__(self, base_url: str, *, ca_bundle: str | None = None, rps: float = 4.0) -> None:
        self.c = JsonClient(base_url, ca_bundle=ca_bundle, rps=rps)

    def close(self) -> None:
        self.c.close()

    def trades_page(self, **params: Any) -> list[dict[str, Any]]:
        data = self.c.get("/trades", params)
        return list(data) if isinstance(data, list) else []

    def trades(
        self,
        condition_id: str,
        *,
        page_limit: int = 1000,
        taker_only: bool = True,
        offset_cap: int = 10_000,
        **extra: Any,
    ) -> Iterator[list[dict[str, Any]]]:
        offset = 0
        while True:
            page = self.trades_page(
                market=condition_id,
                limit=page_limit,
                offset=offset,
                takerOnly=str(taker_only).lower(),
                **extra,
            )
            yield page
            if len(page) < page_limit:
                return
            offset += page_limit
            if offset >= offset_cap:
                raise TruncatedError(condition_id, offset)


class TruncatedError(RuntimeError):
    def __init__(self, condition_id: str, offset: int) -> None:
        super().__init__(
            f"Data API offset cap reached for {condition_id} at offset {offset}; use on-chain fills"
        )
        self.condition_id = condition_id
        self.offset = offset
