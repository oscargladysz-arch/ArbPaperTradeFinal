#!/usr/bin/env python3
"""US reachability probe (spec section 3, Phase 0 blocker; ASSUMPTIONS NET-2).

Run this on Oscar's machine and on an AWS us-east-1 instance with NO VPN or proxy:

    python3 scripts/probe_reachability.py > probe_$(hostname).json

Standard library only, TLS verified with the system trust store. Read-only GET requests and
one WebSocket subscribe (if the `websockets` package is installed). It never sends credentials.
"""

from __future__ import annotations

import asyncio
import json
import platform
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

REST_TARGETS = [
    # Polymarket Global, read-only
    ("polymarket_gamma_markets", "https://gamma-api.polymarket.com/markets?limit=1&closed=true"),
    ("polymarket_gamma_events", "https://gamma-api.polymarket.com/events?limit=1"),
    ("polymarket_clob_time", "https://clob.polymarket.com/time"),
    ("polymarket_clob_ok", "https://clob.polymarket.com/"),
    ("polymarket_data_trades", "https://data-api.polymarket.com/trades?limit=1"),
    ("polymarket_geoblock", "https://polymarket.com/api/geoblock"),
    # Kalshi public, both candidate hosts for /historical/*
    ("kalshi_status", "https://api.elections.kalshi.com/trade-api/v2/exchange/status"),
    ("kalshi_trades", "https://api.elections.kalshi.com/trade-api/v2/markets/trades?limit=1"),
    (
        "kalshi_fee_changes",
        "https://api.elections.kalshi.com/trade-api/v2/series/fee_changes?show_historical=true",
    ),
    (
        "kalshi_hist_cutoff_elections",
        "https://api.elections.kalshi.com/trade-api/v2/historical/cutoff",
    ),
    (
        "kalshi_hist_cutoff_external",
        "https://external-api.kalshi.com/trade-api/v2/historical/cutoff",
    ),
    (
        "kalshi_hist_markets_elections",
        "https://api.elections.kalshi.com/trade-api/v2/historical/markets?limit=1",
    ),
    (
        "kalshi_hist_markets_external",
        "https://external-api.kalshi.com/trade-api/v2/historical/markets?limit=1",
    ),
    # Reference data
    ("fred_dtb3_csv", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"),
    ("public_ip", "https://api.ipify.org?format=json"),
]

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def probe_rest(name: str, url: str) -> dict:
    ctx = ssl.create_default_context()  # verification ON, always
    req = urllib.request.Request(
        url, headers={"User-Agent": "ref-mm-probe/0.1 (read-only)", "Accept": "*/*"}
    )
    t0 = time.perf_counter()
    out: dict = {"name": name, "url": url}
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            body = resp.read(4000)
            out.update(
                status=resp.status,
                ms=round((time.perf_counter() - t0) * 1000),
                body_head=body[:400].decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as e:
        body = e.read(2000).decode("utf-8", "replace")
        out.update(
            status=e.code,
            ms=round((time.perf_counter() - t0) * 1000),
            body_head=body[:400],
            error="http",
        )
    except Exception as e:  # noqa: BLE001
        out.update(status=None, ms=round((time.perf_counter() - t0) * 1000), error=repr(e)[:300])
    head = (out.get("body_head") or "").lower()
    out["geoblock_hint"] = any(
        k in head for k in ("geoblock", "restricted", "not available in your", "forbidden")
    )
    return out


async def probe_ws() -> dict:
    try:
        import websockets  # type: ignore
    except ImportError:
        return {
            "name": "polymarket_ws",
            "url": WS_URL,
            "skipped": "pip install websockets to test the market WebSocket",
        }
    # Find one live token id from Gamma so the subscribe is real.
    token = None
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(
            "https://gamma-api.polymarket.com/markets?limit=1&closed=false&active=true",
            timeout=20,
            context=ctx,
        ) as r:
            m = json.loads(r.read())[0]
            ids = m.get("clobTokenIds")
            ids = json.loads(ids) if isinstance(ids, str) else ids
            token = str(ids[0])
    except Exception as e:  # noqa: BLE001
        return {
            "name": "polymarket_ws",
            "url": WS_URL,
            "error": f"could not fetch a token id: {e!r}",
        }
    t0 = time.perf_counter()
    try:
        async with websockets.connect(
            WS_URL, ssl=ssl.create_default_context(), open_timeout=20
        ) as ws:
            await ws.send(json.dumps({"assets_ids": [token], "type": "market"}))
            msg = await asyncio.wait_for(ws.recv(), timeout=20)
            return {
                "name": "polymarket_ws",
                "url": WS_URL,
                "ok": True,
                "ms": round((time.perf_counter() - t0) * 1000),
                "first_msg_head": str(msg)[:300],
            }
    except Exception as e:  # noqa: BLE001
        return {
            "name": "polymarket_ws",
            "url": WS_URL,
            "ok": False,
            "ms": round((time.perf_counter() - t0) * 1000),
            "error": repr(e)[:300],
        }


def main() -> int:
    report = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "note": "no VPN, no proxy, TLS verified; read-only",
        "rest": [probe_rest(n, u) for n, u in REST_TARGETS],
        "ws": asyncio.run(probe_ws()),
    }
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
