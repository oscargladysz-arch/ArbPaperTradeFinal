# ASSUMPTIONS.md

Rule 4: every API, fee, data-source, and settlement assumption is verified against current
official docs before it is coded, and logged here with source URL and date. Observed behavior
beats docs; discrepancies are logged in the Notes column.

Status values:
- VERIFIED: read on the official page on the given date.
- PARTIAL: confirmed only through web-search snippets of the official page (the cloud session's
  network policy blocks the docs hosts). Must be re-verified from the official page before the
  Phase 0 report ships.
- OBSERVED: confirmed by a live request, JSON saved under `docs/observed/`.
- OPEN: not verified. Code that depends on it names the id in a comment.

Owner is Oscar where the fact needs his machine, an account, or a decision.

## Network and environment

| id | Statement | Status | Source | Date | Notes |
|---|---|---|---|---|---|
| NET-1 | The Claude cloud container cannot reach api.elections.kalshi.com, gamma-api.polymarket.com, data-api.polymarket.com, clob.polymarket.com, docs.kalshi.com, docs.polymarket.com, predexon.com, api.stlouisfed.org, karlwhelan.com, cepr.org, ssrn.com, crypto.clickhouse.com, dune.com (proxy CONNECT 403). pypi.org works. | OBSERVED | curl from the session | 2026-09-23 | Oscar widens the environment's network access or runs loaders elsewhere. |
| NET-2 | Polymarket Global public REST and market WebSocket respond to plain US traffic from Oscar's machine and from AWS us-east-1. | OPEN (Oscar) | run `scripts/probe_reachability.py` | | Spec Phase 0 blocker. |
| NET-3 | Polymarket publishes a geoblock policy page; some endpoints may refuse restricted regions. | PARTIAL | https://docs.polymarket.com/api-reference/geoblock | 2026-09-23 | Read the page once reachable; record which endpoints are affected. |

## Kalshi API

| id | Statement | Status | Source | Date | Notes |
|---|---|---|---|---|---|
| K-API-1 | Live REST base is `https://api.elections.kalshi.com/trade-api/v2`; public market data needs no key. | PARTIAL | notebook Cell 7 worked against it; https://docs.kalshi.com/api-reference | 2026-09-23 | |
| K-API-2 | Which host serves `/historical/*`. A docs snippet shows `https://external-api.kalshi.com/trade-api/v2/historical/markets/{ticker}/candlesticks`. | OPEN | https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks | 2026-09-23 | `KALSHI_HISTORICAL_BASE` env var switches it. Probe both. |
| K-API-3 | Public read rate limit. | OPEN | | | Client defaults to 5 requests/second. |
| K-API-4 | `GET /series/{ticker}` returns `fee_type` and `fee_multiplier`; `GET /series` lists series with category. | PARTIAL | https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes (fields seen: series_ticker, fee_type "quadratic", fee_multiplier, scheduled_ts) | 2026-09-23 | |
| K-API-5 | `GET /events` paginates with `cursor`, `limit` up to 200, `status`, `with_nested_markets`. | PARTIAL | notebook Cell 7 observed behavior (status=open) | 2026-09-23 | `status=settled` unverified. |
| K-API-6 | `GET /markets` paginates with `cursor`, `limit` up to 1000, filters `status`, `series_ticker`, `event_ticker`, `min_close_ts`, `max_close_ts`. | OPEN | | | |
| K-API-7 | `GET /markets/trades` returns public trades with `trade_id, ticker, count, yes_price, no_price, taker_side, created_time`, newer responses add `yes_price_dollars`, `no_price_dollars`, `count_fp`; filters `ticker, min_ts, max_ts, cursor, limit`. `taker_side` is "yes" or "no". | OPEN | | | Normalizer prefers `*_dollars` fields (sub-penny). |
| K-API-8 | 1-minute candles: `GET /series/{series}/markets/{ticker}/candlesticks?start_ts&end_ts&period_interval=1`, fields `end_period_ts`, `yes_bid{open,high,low,close}`, `yes_ask{...}`, `price{...}`, `volume`, `open_interest`; bid/ask carry no size. | PARTIAL | https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks (field names seen for the historical variant) | 2026-09-23 | Max span per request unknown. |
| K-API-9 | `GET /historical/cutoff` returns the boundary; markets settled before it live under `/historical/*`. | PARTIAL | web snippet citing the docs | 2026-09-23 | Loaders call it on every run. |
| K-API-10 | `GET /historical/markets` lists archived markets with cursor pagination. | PARTIAL | web snippet | 2026-09-23 | filters unknown |
| K-API-11 | `GET /historical/trades` returns archived public trades with the same fields as K-API-7. | PARTIAL | web snippet | 2026-09-23 | |
| K-API-12 | `GET /historical/markets/{ticker}/candlesticks` supports `period_interval=1` for archived markets and retains full history. | OPEN | | | If only coarse bars survive archiving, Tier 1 changes. |
| K-API-13 | Sub-penny pricing: some markets quote in fractions of a cent; `tick_size` (or equivalent) is a market field. | OPEN | | | Money is stored at 1/100 cent resolution regardless. |
| K-API-14 | Market fields `result` ("yes"/"no"), `settlement_ts` or equivalent, `expiration_time`, `close_time`, `rules_primary`, `rules_secondary`, `yes_sub_title`, `no_sub_title`, `category`. | OPEN | | | Needed for mapping and settlement returns. |

## Kalshi fees

| id | Statement | Status | Source | Date | Notes |
|---|---|---|---|---|---|
| K-FEE-1 | Taker fee = round_up_to_cent(M_taker * 0.07 * C * P * (1 - P)) per order. Example: 100 contracts at 0.50: 0.07 * 100 * 0.25 = 1.75 dollars. | PARTIAL | https://kalshi.com/docs/kalshi-fee-schedule.pdf (July 2026 schedule) | 2026-09-23 | Legacy engine.py used the same formula. |
| K-FEE-2 | Maker fee = round_up_to_cent(M_maker * 0.0175 * C * P * (1 - P)) per order; default M_maker = 0; several sports series have M_maker = 1 in the July 2026 schedule. Maker rate is 25% of taker. | PARTIAL | same PDF; https://www.botforkalshi.com/blog/kalshi-fees-explained | 2026-09-23 | Round-up is per order, so a 1-contract fill at 0.44 pays 0.01, not 0.0043. See PREREGISTRATION fee section. |
| K-FEE-3 | `GET /series/fee_changes?show_historical=true` returns scheduled and past multiplier changes with `scheduled_ts`. | PARTIAL | https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes | 2026-09-23 | |
| K-FEE-4 | `GET /events/fee_changes` returns event-level overrides. | OPEN | | | Spec names it. |
| K-FEE-5 | Only `fee_type = "quadratic"` is implemented. Any other fee_type raises. | code | pmcore/fees/kalshi.py | 2026-09-23 | Implement others only after reading the schedule. |
| K-FEE-6 | Fee waivers (promotional zero-fee windows) exist and are dated. | OPEN | | | Source unknown; ask Kalshi support docs. |

## Polymarket

| id | Statement | Status | Source | Date | Notes |
|---|---|---|---|---|---|
| P-API-1 | Gamma `GET /markets` supports `closed=true`, `limit`, `offset`, `order`, `ascending`, `end_date_min`, `end_date_max`. A keyset (id-based) cursor may or may not exist. | OPEN | https://docs.polymarket.com | | Loader pages by narrow end-date windows to stay under any offset cap. |
| P-API-2 | Data API `GET /trades?market=<conditionId>&limit&offset&takerOnly` returns `side` (BUY/SELL, taker perspective), `asset` (token id), `outcome`, `outcomeIndex`, `price`, `size`, `timestamp` (seconds), `transactionHash`. Offset is capped at 10,000. | OPEN | | | Loader raises TruncatedError at the cap and marks the market for on-chain. |
| P-API-3 | Which Data API time filters are honored. | OPEN | | | Loader measures by requesting a window and checking timestamps. |
| P-API-4 | Market `outcomes` and `clobTokenIds` are parallel arrays; the YES/NO or team labels come only from `outcomes`. Token index is never a side. | PARTIAL | notebook and resolvers parse both as JSON strings | 2026-09-23 | Mapper uses labels only. |
| P-FEE-1 | Polymarket Global fee rollout across most categories took effect 2026-03-30 (regime break for Section 4). | PARTIAL | https://www.bitget.com/news/detail/12560605296449 ; https://defirate.com/news/polymarket-revenue-surges-10x-fee-rollout-usd-token-platform-overhaul-ahead/ | 2026-09-23 | Verify on Polymarket's own changelog. |
| P-CLOB-1 | CLOB V2 launched 2026-04-28 (regime break). | OPEN | | | Not found by web search. |
| P-US-1 | Polymarket US is a separate CFTC-regulated exchange (QCX acquisition, Nov 2025) with its own fee schedule from 2026-04-03 and mandatory KYC. Global remains closed to US trading. | PARTIAL | https://www.quantvps.com/blog/polymarket-us-api-available ; https://www.polysyncer.com/blog/polymarket-us-trading | 2026-09-23 | Fallback only if NET-2 fails. Read-only use of Global stays within rule 10. |
| P-HIST-1 | `/prices-history` is not used for historical FV (resolved markets return 12-hour bars; 1-minute series behaves like a mirrored midpoint). | spec | spec section 3 | 2026-09-23 | Not used anywhere. |
| P-CHAIN-1 | On-chain CTF Exchange fills (CLOB V1 and V2 contracts) are queryable via ClickHouse CryptoHouse, Dune, or Goldsky. | OPEN (Oscar chooses) | | | Primary source where P-API-2 truncates. |

## Vendors and references

| id | Statement | Status | Source | Date | Notes |
|---|---|---|---|---|---|
| PRED-1 | Predexon sells raw tick-level order-book deltas as Parquet for Polymarket and Kalshi, pay per GiB, free quotes, finalized daily. | PARTIAL | https://predexon.com/ ; https://docs.predexon.com/data-signals/overview | 2026-09-23 | $40/GiB, $80/GiB, $5 minimum, $50 credits are unverified. |
| KAT-1 | "Makers and Takers: The Economics of the Kalshi Prediction Market", Bürgi, Deng, Whelan, CEPR DP20631, Sept 2025: takers lose about 32%, makers about 10%. | PARTIAL | https://cepr.org/publications/dp20631 ; https://www.karlwhelan.com/Papers/Kalshi.pdf ; https://cepr.org/voxeu/columns/economics-kalshi-prediction-market | 2026-09-23 | Sample window, universe, and return definition still needed (PDF blocked). Oscar drops the PDF in docs/refs/. |
| FRED-1 | DTB3 (3-month T-bill secondary market rate) via `https://api.stlouisfed.org/fred/series/observations?series_id=DTB3&api_key=...` or the CSV export `https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3`. | OPEN | | | Loader supports both. |
| HFT-1 | hftbacktest (github.com/nkaz001/hftbacktest) supports Python 3.12, multi-asset feeds, configurable latency and queue models. | OPEN | | | Phase 3. |
