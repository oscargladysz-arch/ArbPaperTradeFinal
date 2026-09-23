"""Polymarket Data API v2 trades client. Read-only (rule 10).

VERIFIED 2026-09-23 (docs/refs/data-api-v2-openapi.json, live test in docs/observed/):
- GET /v2/trades?condition=<id>&limit=1000 returns {data, pagination{next_cursor, has_more}};
  the cursor seeks on (block_timestamp, sequence_id) so deep pages cost the same as shallow ones.
  40 pages * 1000 rows paged in 20 s with no cap.
- The condition shape serves a fixed three-year window; `start`/`end` are honored only on the
  `user` shape. Rows are newest first.
- taker_only defaults to true: each fill once, on its taker side. `side` is the taker's side.
- Row fields: token_id, condition_id, side, price, size, timestamp (seconds), outcome,
  outcome_index (0 or 1), transaction_hash.
The v1 route (/trades with offset) is capped at offset 10,000 (observed 400) and is not used.
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

    def trades(
        self,
        condition_id: str,
        *,
        page_limit: int = 1000,
        taker_only: bool = True,
        max_pages: int | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {
                "condition": condition_id,
                "limit": page_limit,
                "taker_only": str(taker_only).lower(),
            }
            if cursor:
                params["cursor"] = cursor
            data = self.c.get("/v2/trades", params)
            items = list(data.get("data") or [])
            yield items
            pages += 1
            pag = data.get("pagination") or {}
            cursor = pag.get("next_cursor") or None
            if not cursor or not items or (max_pages and pages >= max_pages):
                return


class TruncatedError(RuntimeError):
    """Kept for callers; v2 cursors have shown no cap. Raised only if a page limit is hit."""

    def __init__(self, condition_id: str, offset: int) -> None:
        super().__init__(f"trade pagination stopped early for {condition_id} at {offset}")
        self.condition_id = condition_id
        self.offset = offset
