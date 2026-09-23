"""Kalshi public REST client (no key). Read-only.

Endpoint paths and parameter names below are ASSUMPTIONS until verified against
https://docs.kalshi.com (ids K-API-*). Every method names its assumption id. Observed behavior
is logged by the loaders and, where it differs from docs, recorded in docs/ASSUMPTIONS.md.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pmcore.venues.http import JsonClient


class KalshiPublic:
    def __init__(
        self,
        base_url: str,
        historical_base_url: str | None = None,
        *,
        ca_bundle: str | None = None,
        rps: float = 5.0,
    ) -> None:
        self.live = JsonClient(base_url, ca_bundle=ca_bundle, rps=rps)
        hist = historical_base_url or base_url
        self.hist = (
            self.live if hist == base_url else JsonClient(hist, ca_bundle=ca_bundle, rps=rps)
        )

    def close(self) -> None:
        self.live.close()
        if self.hist is not self.live:
            self.hist.close()

    # -- pagination ---------------------------------------------------------------------
    @staticmethod
    def _paginate(
        client: JsonClient, path: str, params: dict[str, Any], key: str, page_limit: int
    ) -> Iterator[list[dict[str, Any]]]:
        cursor: str | None = None
        while True:
            data = client.get(path, {**params, "limit": page_limit, "cursor": cursor})
            items = data.get(key, []) or []
            yield items
            cursor = data.get("cursor") or None
            if not cursor or not items:
                return

    # -- exchange -----------------------------------------------------------------------
    def exchange_status(self) -> Any:
        return self.live.get("/exchange/status")  # K-API-1

    # -- series and fees ------------------------------------------------------------------
    def series(self, series_ticker: str) -> Any:
        return self.live.get(f"/series/{series_ticker}")  # K-API-4: fee_type, fee_multiplier

    def list_series(self, category: str | None = None) -> Any:
        return self.live.get("/series", {"category": category})  # K-API-4

    def series_fee_changes(self, show_historical: bool = True) -> Any:
        return self.live.get(
            "/series/fee_changes", {"show_historical": str(show_historical).lower()}
        )  # K-FEE-3

    def events_fee_changes(self) -> Any:
        return self.live.get("/events/fee_changes")  # K-FEE-4

    # -- events and markets (live window) ---------------------------------------------------
    def events(
        self,
        status: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = True,
        page_limit: int = 200,
    ) -> Iterator[list[dict[str, Any]]]:
        return self._paginate(
            self.live,
            "/events",
            {
                "status": status,
                "series_ticker": series_ticker,
                "with_nested_markets": str(with_nested_markets).lower(),
            },
            "events",
            page_limit,
        )  # K-API-5

    def markets(
        self,
        status: str | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        page_limit: int = 1000,
    ) -> Iterator[list[dict[str, Any]]]:
        return self._paginate(
            self.live,
            "/markets",
            {
                "status": status,
                "series_ticker": series_ticker,
                "event_ticker": event_ticker,
                "min_close_ts": min_close_ts,
                "max_close_ts": max_close_ts,
            },
            "markets",
            page_limit,
        )  # K-API-6

    def market(self, ticker: str) -> Any:
        return self.live.get(f"/markets/{ticker}")  # K-API-6

    def trades(
        self,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        page_limit: int = 1000,
    ) -> Iterator[list[dict[str, Any]]]:
        """Public trade tape with taker_side (K-API-7)."""
        return self._paginate(
            self.live,
            "/markets/trades",
            {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts},
            "trades",
            page_limit,
        )

    def candlesticks(
        self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1
    ) -> Any:
        return self.live.get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )  # K-API-8

    # -- historical namespace ---------------------------------------------------------------
    def historical_cutoff(self) -> Any:
        return self.hist.get("/historical/cutoff")  # K-API-9

    def historical_markets(
        self, page_limit: int = 1000, **params: Any
    ) -> Iterator[list[dict[str, Any]]]:
        return self._paginate(
            self.hist, "/historical/markets", params, "markets", page_limit
        )  # K-API-10

    def historical_trades(
        self,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        page_limit: int = 1000,
    ) -> Iterator[list[dict[str, Any]]]:
        return self._paginate(
            self.hist,
            "/historical/trades",
            {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts},
            "trades",
            page_limit,
        )  # K-API-11

    def historical_candlesticks(
        self, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1
    ) -> Any:
        return self.hist.get(
            f"/historical/markets/{ticker}/candlesticks",
            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )  # K-API-12
