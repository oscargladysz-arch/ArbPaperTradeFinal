import ssl
ssl._create_default_https_context = ssl._create_unverified_context

# ---- engine.py — Prediction Market Arbitrage Paper Trading Engine ----
#     filter requiring return > risk-free rate for the holding

import asyncio
import base64
import csv
import json
import math
import os
import time
from datetime import datetime, timezone
from sortedcontainers import SortedDict

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "websockets"])
    import websockets

import pandas as pd

from config import (
    KALSHI_API_KEY_ID,
    KALSHI_PRIVATE_KEY_PATH,
    KALSHI_WS_URL,
    POLYMARKET_WS_URL,
    SUBSCRIPTION_MANIFEST_PATH,
    PAPER_LEDGER_PATH,
    TARGET_LIQUIDITY_USD,
    COOLDOWN_SECONDS,
)

# ---- ENGINE SETTINGS ----
MAX_STALENESS_MS = 5000    # Reject trades if either book is older than 5 seconds
MIN_BOOK_LEVELS = 2        # Require at least 2 levels on each side

# ---- PHASE 2: FEE MODEL ----
#   round up to nearest cent of (0.07 * contracts * price * (1 - price))
KALSHI_FEE_COEFFICIENT = 0.07
POLY_FEE_COEFFICIENT = 0.0   # Polymarket: 0% fees currently (hook for future)

# ---- PHASE 3: ANNUALIZED-RETURN FILTER ----
# A trade must beat the risk-free rate for its holding period.
RISK_FREE_RATE_ANNUAL = 0.04   # 4% T-bill
MIN_DAYS_TO_RESOLUTION = 1     # reject if market resolves within 24h
LONG_DATED_ASSUMED_DAYS = 1095  # capped pairs: assume a 3y lockup
MAX_GROSS_SPREAD = 0.15         # spreads past this are stale books, not arbs
OVERSUM_THRESHOLD = 1.05        # kalshi YES prices summing past $1 = mispriced event
OVERSUM_MIN_MARKETS = 3


# ---- PHASE 2: FEE MODEL ----

def kalshi_total_fee(price, contracts):
    """
    Kalshi taker fee for an entire order (not per-contract).
    Formula from Kalshi docs: round up to nearest cent of
      0.07 * contracts * price * (1 - price)
    Returns total fee in dollars.
    """
    if price <= 0 or price >= 1 or contracts <= 0:
        return 0.0
    raw = KALSHI_FEE_COEFFICIENT * contracts * price * (1.0 - price)
    return math.ceil(raw * 100) / 100.0


def kalshi_fee_rate(price, contracts=None):
    """
    Kalshi taker fee as a fraction of notional.

    If contracts is None, returns the ASYMPTOTIC rate (no rounding distortion)
    which is appropriate for large orders:
      fee_rate ≈ 0.07 * (1 - price)

    If contracts is given, returns the actual rate including rounding,
    which matters for small orders.
    """
    if price <= 0 or price >= 1:
        return 0.0
    if contracts is None or contracts <= 0:
        # Asymptotic rate (round-up distortion is negligible at scale)
        return KALSHI_FEE_COEFFICIENT * (1.0 - price)
    # Actual rate with rounding
    total_fee = kalshi_total_fee(price, contracts)
    notional = contracts * price
    return total_fee / notional if notional > 0 else 0.0


def poly_fee_rate(price, contracts=None):
    """Polymarket fee rate (hook — currently 0%)."""
    return POLY_FEE_COEFFICIENT


def round_trip_cost(poly_vwap, kalshi_vwap, target_usd=None):
    """
    Total cost to enter the arbitrage, as a fraction of $1 payout.

    If target_usd is given, uses the actual contracts implied by the VWAP
    to account for Kalshi's penny-rounding on small orders. Otherwise
    uses asymptotic rates (fine for typical position sizes >= $50).

    Entry costs:
      Poly side: vwap * (1 + poly_fee_rate)
      Kalshi side: vwap * (1 + kalshi_fee_rate)

    Note: exit fees on settlement are near-zero because Kalshi's fee
    formula is P*(1-P), which approaches 0 at the tails where resolution
    happens ($1 for winner, $0 for loser). So hold-to-resolution essentially
    pays only entry fees.
    """
    # Contracts implied by deploying target_usd per leg at the VWAP
    poly_contracts = (target_usd / poly_vwap) if (target_usd and poly_vwap > 0) else None
    kalshi_contracts = (target_usd / kalshi_vwap) if (target_usd and kalshi_vwap > 0) else None

    p_fee = poly_fee_rate(poly_vwap, poly_contracts)
    k_fee = kalshi_fee_rate(kalshi_vwap, kalshi_contracts)
    cost = poly_vwap * (1.0 + p_fee) + kalshi_vwap * (1.0 + k_fee)
    return cost


# ---- PHASE 3: ANNUALIZED-RETURN FILTER ----

def passes_time_value_filter(spread_profit, days_to_resolution, date_capped=False):
    """Trade must beat the risk-free rate over its holding period."""
    if days_to_resolution is None or (isinstance(days_to_resolution, float) and math.isnan(days_to_resolution)):
        return False, 0.0, "no_resolution_date"
    if days_to_resolution < MIN_DAYS_TO_RESOLUTION:
        return False, 0.0, f"resolves_too_soon ({days_to_resolution:.1f}d)"
    if date_capped:
        # true horizon unknown -- demand the return of a long lockup
        days_to_resolution = max(days_to_resolution, LONG_DATED_ASSUMED_DAYS)
    required = RISK_FREE_RATE_ANNUAL * (days_to_resolution / 365.0)
    if spread_profit < required:
        return False, required, f"below_risk_free (need {required*100:.2f}%, got {spread_profit*100:.2f}%)"
    return True, required, "pass"


# ---- ORDER BOOK ----

class OrderBook:
    __slots__ = ['bids', 'asks', 'last_update_ms']

    def __init__(self):
        self.bids = SortedDict()
        self.asks = SortedDict()
        self.last_update_ms = 0

    def set_snapshot(self, bids, asks):
        self.bids = SortedDict()
        self.asks = SortedDict()
        for price, size in bids:
            p, s = float(price), float(size)
            if s > 0:
                self.bids[p] = s
        for price, size in asks:
            p, s = float(price), float(size)
            if s > 0:
                self.asks[p] = s
        self.last_update_ms = int(time.time() * 1000)

    def update_level(self, side, price, size):
        book = self.bids if side == 'bid' else self.asks
        p, s = float(price), float(size)
        if s <= 0:
            book.pop(p, None)
        else:
            book[p] = s
        self.last_update_ms = int(time.time() * 1000)

    def update_level_delta(self, side, price, delta):
        book = self.bids if side == 'bid' else self.asks
        p, d = float(price), float(delta)
        current = book.get(p, 0.0)
        new_size = current + d
        if new_size <= 0:
            book.pop(p, None)
        else:
            book[p] = new_size
        self.last_update_ms = int(time.time() * 1000)

    def staleness_ms(self):
        if self.last_update_ms == 0:
            return 999999
        return int(time.time() * 1000) - self.last_update_ms


# ---- VWAP CALCULATOR ----

def vwap_walk(book_side, target_usd, ascending=True):
    if not book_side or target_usd <= 0:
        return None, 0.0, 0

    total_cost = 0.0
    total_shares = 0.0
    levels = 0

    items = book_side.items() if ascending else reversed(book_side.items())

    for price, size in items:
        if price <= 0 or price >= 1:
            continue

        remaining_usd = target_usd - total_cost
        if remaining_usd <= 0:
            break

        levels += 1
        max_cost_at_level = size * price

        if max_cost_at_level <= remaining_usd:
            total_cost += max_cost_at_level
            total_shares += size
        else:
            shares_to_fill = remaining_usd / price
            total_cost += shares_to_fill * price
            total_shares += shares_to_fill

    if total_shares == 0:
        return None, 0.0, 0

    vwap = total_cost / total_shares
    return vwap, total_cost, levels


def compute_kalshi_ask_book(bid_book):
    ask_book = SortedDict()
    for bid_price, size in bid_book.items():
        ask_price = round(1.0 - bid_price, 4)
        if 0 < ask_price < 1:
            ask_book[ask_price] = size
    return ask_book


# ---- MANIFEST FILTER ----

def filter_manifest(df):
    """Remove known bad pair types from manifest before loading."""
    original = len(df)

    # If the manifest is missing the new date columns, fail loudly.
    required_cols = ["days_to_resolution", "resolution_date",
                     "poly_end_date", "kalshi_expiration_time", "date_capped"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise RuntimeError(
            f"Manifest is missing required columns: {missing}. "
            f"Re-run merge_and_verify.py with the Phase 1 resolvers to "
            f"regenerate the manifest with date fields."
        )

    # Remove Polymarket spread markets
    spread_mask = df['poly_question'].str.startswith('Spread:', na=False)
    df = df[~spread_mask].copy()

    # Remove pairs where poly_question contains "More Markets" (duplicate listings)
    more_mask = df['poly_question'].str.contains('More Markets', na=False)
    df = df[~more_mask].copy()

    # Drop pairs missing resolution date — we can't evaluate time-value filter
    no_date_mask = df['days_to_resolution'].isna()
    no_date_count = no_date_mask.sum()
    df = df[~no_date_mask].copy()

    # Drop pairs that resolve too soon (settlement-in-flight risk)
    soon_mask = df['days_to_resolution'] < MIN_DAYS_TO_RESOLUTION
    soon_count = soon_mask.sum()
    df = df[~soon_mask].copy()

    removed = original - len(df)
    if removed > 0:
        print(f"  Filtered out {removed} pairs:")
        print(f"    Missing resolution date: {no_date_count}")
        print(f"    Resolves within {MIN_DAYS_TO_RESOLUTION}d: {soon_count}")
        print(f"    Spreads/duplicates:      {removed - no_date_count - soon_count}")
    return df.reset_index(drop=True)


# ---- PAIR MANAGER ----

class PairManager:
    def __init__(self, manifest_df, target_usd, cooldown_sec, ledger_path):
        self.target_usd = target_usd
        self.cooldown_sec = cooldown_sec
        self.ledger_path = ledger_path

        self.poly_books = {}
        self.kalshi_books = {}
        self.poly_token_to_pairs = {}
        self.kalshi_ticker_to_pairs = {}
        self.kalshi_event_to_tickers = {}
        self.pairs = []
        self.cooldowns = {}

        self.evaluations = 0
        self.trades_logged = 0
        self.skipped_stale = 0
        self.skipped_thin = 0
        self.skipped_time_value = 0
        self.skipped_suspect = 0
        self.skipped_oversum = 0
        self.start_time = time.time()

        self._init_ledger()
        self._build_pairs(manifest_df)

    def _init_ledger(self):
        if not os.path.exists(self.ledger_path):
            os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
            with open(self.ledger_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'poly_event_title', 'kalshi_event_title',
                    'poly_question', 'kalshi_ticker',
                    'direction', 'poly_vwap', 'kalshi_vwap',
                    'total_cost', 'projected_profit',
                    'capital_deployed', 'poly_staleness_ms', 'kalshi_staleness_ms',
                    # timing + fee fields
                    'days_to_resolution', 'required_return_for_period',
                    'annualized_return', 'resolution_date',
                    'poly_fee_pct', 'kalshi_fee_pct',
                ])

    def _build_pairs(self, df):
        for _, row in df.iterrows():
            days = row.get('days_to_resolution')
            if pd.isna(days):
                continue

            pair = {
                'index': len(self.pairs),
                'poly_yes_token': str(row['poly_yes_token']),
                'poly_no_token': str(row['poly_no_token']),
                'kalshi_ticker': str(row['kalshi_ticker']),
                'kalshi_event_ticker': str(row.get('kalshi_event_ticker', '')),
                'poly_question': str(row.get('poly_question', '')),
                'poly_event_title': str(row.get('poly_event_title', '')),
                'kalshi_event_title': str(row.get('kalshi_event_title', '')),
                'category': str(row.get('category', '')),
                'days_to_resolution': float(days),
                'resolution_date': str(row.get('resolution_date', '')),
                'date_capped': str(row.get('date_capped', False)).lower() == 'true',
            }
            idx = pair['index']
            self.pairs.append(pair)

            for token in [pair['poly_yes_token'], pair['poly_no_token']]:
                if token not in self.poly_books:
                    self.poly_books[token] = OrderBook()
                if token not in self.poly_token_to_pairs:
                    self.poly_token_to_pairs[token] = []
                self.poly_token_to_pairs[token].append(idx)

            ticker = pair['kalshi_ticker']
            if ticker not in self.kalshi_books:
                self.kalshi_books[ticker] = {
                    'yes': OrderBook(),
                    'no': OrderBook(),
                }
            if ticker not in self.kalshi_ticker_to_pairs:
                self.kalshi_ticker_to_pairs[ticker] = []
            self.kalshi_ticker_to_pairs[ticker].append(idx)

            evt = pair['kalshi_event_ticker']
            if evt:
                self.kalshi_event_to_tickers.setdefault(evt, set()).add(ticker)

    # ---- MESSAGE HANDLERS ----

    def handle_poly_book(self, data):
        asset_id = str(data.get('asset_id', ''))
        if asset_id not in self.poly_books:
            return
        bids = [(b['price'], b['size']) for b in data.get('bids', [])]
        asks = [(a['price'], a['size']) for a in data.get('asks', [])]
        self.poly_books[asset_id].set_snapshot(bids, asks)
        for pair_idx in self.poly_token_to_pairs.get(asset_id, []):
            self._evaluate_pair(pair_idx)

    def handle_poly_price_change(self, data):
        changes = data.get('price_changes', data.get('changes', []))
        if not changes:
            return
        affected_pairs = set()
        for change in changes:
            asset_id = str(change.get('asset_id', ''))
            if asset_id not in self.poly_books:
                continue
            price = change.get('price', '0')
            size = change.get('size', '0')
            side_raw = change.get('side', '').upper()
            side = 'bid' if side_raw == 'BUY' else 'ask'
            self.poly_books[asset_id].update_level(side, price, size)
            for pair_idx in self.poly_token_to_pairs.get(asset_id, []):
                affected_pairs.add(pair_idx)
        for pair_idx in affected_pairs:
            self._evaluate_pair(pair_idx)

    def handle_kalshi_snapshot(self, msg):
        ticker = msg.get('market_ticker', '')
        if ticker not in self.kalshi_books:
            return
        yes_levels = msg.get('yes_dollars_fp', [])
        no_levels = msg.get('no_dollars_fp', [])
        self.kalshi_books[ticker]['yes'].set_snapshot(
            [(p, s) for p, s in yes_levels], []
        )
        self.kalshi_books[ticker]['no'].set_snapshot(
            [(p, s) for p, s in no_levels], []
        )
        for pair_idx in self.kalshi_ticker_to_pairs.get(ticker, []):
            self._evaluate_pair(pair_idx)

    def handle_kalshi_delta(self, msg):
        ticker = msg.get('market_ticker', '')
        if ticker not in self.kalshi_books:
            return
        price = msg.get('price_dollars', '0')
        delta = msg.get('delta_fp', '0')
        side = msg.get('side', '')
        if side in ('yes', 'no'):
            self.kalshi_books[ticker][side].update_level_delta('bid', price, delta)
        for pair_idx in self.kalshi_ticker_to_pairs.get(ticker, []):
            self._evaluate_pair(pair_idx)

    def _event_oversum(self, event_ticker):
        """Skip events whose Kalshi YES prices sum well past $1 (mispriced long-tail books)."""
        tickers = self.kalshi_event_to_tickers.get(event_ticker, ())
        if len(tickers) < OVERSUM_MIN_MARKETS:
            return False
        total, counted = 0.0, 0
        for t in tickers:
            books = self.kalshi_books.get(t)
            if not books or not books['yes'].bids:
                continue
            total += books['yes'].bids.peekitem(-1)[0]
            counted += 1
        return counted >= OVERSUM_MIN_MARKETS and total > OVERSUM_THRESHOLD

    # ---- SPREAD EVALUATION ----

    def _evaluate_pair(self, pair_idx):
        self.evaluations += 1
        pair = self.pairs[pair_idx]

        last_trade = self.cooldowns.get(pair_idx, 0)
        if time.time() - last_trade < self.cooldown_sec:
            return

        poly_yes_book = self.poly_books.get(pair['poly_yes_token'])
        poly_no_book = self.poly_books.get(pair['poly_no_token'])
        kalshi_books = self.kalshi_books.get(pair['kalshi_ticker'])
        if not poly_yes_book or not poly_no_book or not kalshi_books:
            return

        if poly_yes_book.last_update_ms == 0 or kalshi_books['yes'].last_update_ms == 0:
            return

        # sanity: skip events where kalshi candidates are collectively overpriced
        if self._event_oversum(pair['kalshi_event_ticker']):
            self.skipped_oversum += 1
            return

        days = pair['days_to_resolution']
        capped = pair['date_capped']

        # scenario A: buy poly YES + kalshi NO
        poly_stale_a = poly_yes_book.staleness_ms()
        kalshi_stale_a = kalshi_books['yes'].staleness_ms()

        if poly_stale_a <= MAX_STALENESS_MS and kalshi_stale_a <= MAX_STALENESS_MS:
            kalshi_no_asks = compute_kalshi_ask_book(kalshi_books['yes'].bids)

            if len(poly_yes_book.asks) >= MIN_BOOK_LEVELS and len(kalshi_no_asks) >= MIN_BOOK_LEVELS:
                poly_yes_vwap, _, _ = vwap_walk(poly_yes_book.asks, self.target_usd, ascending=True)
                kalshi_no_vwap, _, _ = vwap_walk(kalshi_no_asks, self.target_usd, ascending=True)

                if poly_yes_vwap and kalshi_no_vwap:
                    cost_a = round_trip_cost(poly_yes_vwap, kalshi_no_vwap, self.target_usd)
                    if cost_a < 1.0:
                        profit_a = 1.0 - cost_a
                        if profit_a > MAX_GROSS_SPREAD:
                            # too good to be true = stale book or bad pair
                            self.skipped_suspect += 1
                        else:
                            passes, required, _ = passes_time_value_filter(profit_a, days, capped)
                            if passes:
                                self._log_trade(
                                    pair, 'PolyYES_KalshiNO',
                                    poly_yes_vwap, kalshi_no_vwap,
                                    cost_a, profit_a,
                                    poly_stale_a, kalshi_stale_a,
                                    required,
                                )
                                return
                            self.skipped_time_value += 1
            else:
                self.skipped_thin += 1
        else:
            self.skipped_stale += 1

        # scenario B: buy poly NO + kalshi YES
        poly_stale_b = poly_no_book.staleness_ms()
        kalshi_stale_b = kalshi_books['no'].staleness_ms()

        if poly_stale_b <= MAX_STALENESS_MS and kalshi_stale_b <= MAX_STALENESS_MS:
            kalshi_yes_asks = compute_kalshi_ask_book(kalshi_books['no'].bids)

            if len(poly_no_book.asks) >= MIN_BOOK_LEVELS and len(kalshi_yes_asks) >= MIN_BOOK_LEVELS:
                poly_no_vwap, _, _ = vwap_walk(poly_no_book.asks, self.target_usd, ascending=True)
                kalshi_yes_vwap, _, _ = vwap_walk(kalshi_yes_asks, self.target_usd, ascending=True)

                if poly_no_vwap and kalshi_yes_vwap:
                    cost_b = round_trip_cost(poly_no_vwap, kalshi_yes_vwap, self.target_usd)
                    if cost_b < 1.0:
                        profit_b = 1.0 - cost_b
                        if profit_b > MAX_GROSS_SPREAD:
                            self.skipped_suspect += 1
                        else:
                            passes, required, _ = passes_time_value_filter(profit_b, days, capped)
                            if passes:
                                self._log_trade(
                                    pair, 'PolyNO_KalshiYES',
                                    poly_no_vwap, kalshi_yes_vwap,
                                    cost_b, profit_b,
                                    poly_stale_b, kalshi_stale_b,
                                    required,
                                )
                            else:
                                self.skipped_time_value += 1
            else:
                self.skipped_thin += 1
        else:
            self.skipped_stale += 1

    def _log_trade(self, pair, direction, poly_vwap, kalshi_vwap,
                   total_cost, profit, poly_stale, kalshi_stale,
                   required_return):
        self.trades_logged += 1
        self.cooldowns[pair['index']] = time.time()

        ts = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        dollar_profit = profit * self.target_usd
        days = pair['days_to_resolution']

        # Annualized return (simple, for logging/analysis, not filtering)
        # profit is fractional; annualize linearly to match filter convention
        annualized = profit * (365.0 / days) if days > 0 else 0.0

        # Per-leg fee rates used (with actual contract counts for accuracy)
        poly_contracts = (self.target_usd / poly_vwap) if poly_vwap > 0 else 0
        kalshi_contracts = (self.target_usd / kalshi_vwap) if kalshi_vwap > 0 else 0
        poly_fee_pct = poly_fee_rate(poly_vwap, poly_contracts)
        kalshi_fee_pct = kalshi_fee_rate(kalshi_vwap, kalshi_contracts)

        row = [
            ts,
            pair['poly_event_title'][:80],
            pair['kalshi_event_title'][:80],
            pair['poly_question'][:80],
            pair['kalshi_ticker'],
            direction,
            f"{poly_vwap:.4f}",
            f"{kalshi_vwap:.4f}",
            f"{total_cost:.4f}",
            f"{profit:.4f}",
            f"{self.target_usd:.2f}",
            str(poly_stale),
            str(kalshi_stale),
            # timing + fee fields
            f"{days:.2f}",
            f"{required_return:.4f}",
            f"{annualized:.4f}",
            pair.get('resolution_date', ''),
            f"{poly_fee_pct:.4f}",
            f"{kalshi_fee_pct:.4f}",
        ]

        with open(self.ledger_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)

        print(f"\n  {'=' * 55}")
        print(f"  PAPER TRADE #{self.trades_logged}")
        print(f"  {'-' * 55}")
        print(f"  Event:     {pair['poly_event_title'][:60]}")
        print(f"  Question:  {pair['poly_question'][:60]}")
        print(f"  Direction: {direction}")
        print(f"  Poly VWAP: {poly_vwap:.4f}  (fee {poly_fee_pct*100:.2f}%)")
        print(f"  Kalshi VWAP: {kalshi_vwap:.4f}  (fee {kalshi_fee_pct*100:.2f}%)")
        print(f"  Total cost: {total_cost:.4f}  (dynamic fee model)")
        print(f"  Profit:    ${dollar_profit:.2f} on ${self.target_usd} deployed ({profit*100:.2f}%)")
        print(f"  Days to resolution: {days:.1f}  (annualized: {annualized*100:.1f}%)")
        print(f"  Required for risk-free: {required_return*100:.2f}%")
        print(f"  Staleness: Poly={poly_stale}ms  Kalshi={kalshi_stale}ms")
        print(f"  {'=' * 55}")

    def print_status(self):
        elapsed = time.time() - self.start_time
        poly_active = sum(1 for b in self.poly_books.values() if b.last_update_ms > 0)
        kalshi_active = sum(
            1 for books in self.kalshi_books.values()
            if books['yes'].last_update_ms > 0 or books['no'].last_update_ms > 0
        )
        print(
            f"  [STATUS] {elapsed:.0f}s | "
            f"Evals: {self.evaluations:,} | "
            f"Trades: {self.trades_logged} | "
            f"Stale-skip: {self.skipped_stale:,} | "
            f"Thin-skip: {self.skipped_thin:,} | "
            f"TV-skip: {self.skipped_time_value:,} | "
            f"Suspect: {self.skipped_suspect:,} | "
            f"Oversum: {self.skipped_oversum:,} | "
            f"Poly: {poly_active}/{len(self.poly_books)} | "
            f"Kalshi: {kalshi_active}/{len(self.kalshi_books)}"
        )


# ---- SSL + AUTH ----

def make_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def load_private_key():
    with open(KALSHI_PRIVATE_KEY_PATH, 'rb') as f:
        return serialization.load_pem_private_key(f.read(), password=None)

def sign_pss_text(private_key, text: str) -> str:
    message = text.encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')

def create_kalshi_ws_headers(private_key) -> dict:
    timestamp = str(int(time.time() * 1000))
    msg_string = timestamp + "GET" + "/trade-api/ws/v2"
    signature = sign_pss_text(private_key, msg_string)
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


# ---- WEBSOCKET LISTENERS ----

async def polymarket_stream(manager, token_ids):
    BATCH_SIZE = 500
    batches = [token_ids[i:i+BATCH_SIZE] for i in range(0, len(token_ids), BATCH_SIZE)]

    async def listen_batch(batch, batch_num):
        while True:
            try:
                print(f"  [POLY-{batch_num}] Connecting ({len(batch)} tokens)...")
                async with websockets.connect(
                    POLYMARKET_WS_URL,
                    ssl=make_ssl_context(),
                    ping_interval=10,
                    ping_timeout=30,
                    max_size=10_000_000,
                ) as ws:
                    subscribe_msg = {"assets_ids": batch, "type": "market"}
                    await ws.send(json.dumps(subscribe_msg))
                    print(f"  [POLY-{batch_num}] ✓ Subscribed")

                    async for raw_msg in ws:
                        try:
                            raw = json.loads(raw_msg)
                            items = raw if isinstance(raw, list) else [raw]
                            for data in items:
                                if not isinstance(data, dict):
                                    continue
                                evt = data.get('event_type', '')
                                if evt == 'book':
                                    manager.handle_poly_book(data)
                                elif evt == 'price_change':
                                    manager.handle_poly_price_change(data)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                print(f"  [POLY-{batch_num}] Disconnected: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    tasks = [listen_batch(batch, i+1) for i, batch in enumerate(batches)]
    await asyncio.gather(*tasks)


async def kalshi_stream(manager, market_tickers):
    while True:
        try:
            private_key = load_private_key()
            headers = create_kalshi_ws_headers(private_key)

            print(f"  [KALSHI] Connecting ({len(market_tickers)} tickers)...")
            async with websockets.connect(
                KALSHI_WS_URL,
                additional_headers=headers,
                ssl=make_ssl_context(),
                max_size=10_000_000,
            ) as ws:
                KALSHI_BATCH = 100
                for i in range(0, len(market_tickers), KALSHI_BATCH):
                    batch = market_tickers[i:i+KALSHI_BATCH]
                    subscribe_msg = {
                        "id": i // KALSHI_BATCH + 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta"],
                            "market_tickers": batch,
                        }
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    await asyncio.sleep(0.1)

                print(f"  [KALSHI] ✓ Subscribed to {len(market_tickers)} markets")

                async for raw_msg in ws:
                    try:
                        data = json.loads(raw_msg)
                        msg_type = data.get('type', '')
                        msg = data.get('msg', {})
                        if msg_type == 'orderbook_snapshot':
                            manager.handle_kalshi_snapshot(msg)
                        elif msg_type == 'orderbook_delta':
                            manager.handle_kalshi_delta(msg)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"  [KALSHI] Disconnected: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


async def status_printer(manager, interval=30):
    while True:
        await asyncio.sleep(interval)
        manager.print_status()


# ---- MAIN ----

async def main():
    print("=" * 60)
    print("ARBITRAGE PAPER TRADING ENGINE v3")
    print("  (Dynamic fee model + time-value filter)")
    print("=" * 60)

    df = pd.read_csv(SUBSCRIPTION_MANIFEST_PATH)
    print(f"  Loaded {len(df)} pairs from manifest")

    # Filter bad pairs (requires new date columns — fails loudly if missing)
    df = filter_manifest(df)
    print(f"  Active pairs: {len(df)}")

    # Clear previous ledger
    if os.path.exists(PAPER_LEDGER_PATH):
        os.remove(PAPER_LEDGER_PATH)
        print(f"  Cleared previous ledger")

    manager = PairManager(
        df,
        target_usd=TARGET_LIQUIDITY_USD,
        cooldown_sec=COOLDOWN_SECONDS,
        ledger_path=PAPER_LEDGER_PATH,
    )

    poly_token_ids = list(manager.poly_books.keys())
    kalshi_tickers = list(manager.kalshi_books.keys())

    print(f"\n  Polymarket tokens:  {len(poly_token_ids)}")
    print(f"  Kalshi tickers:     {len(kalshi_tickers)}")
    print(f"  Target liquidity:   ${TARGET_LIQUIDITY_USD}")
    print(f"  Fee model:          DYNAMIC (Kalshi: 0.07*C*P*(1-P) round-up-cent, Poly: 0%)")
    print(f"  Risk-free rate:     {RISK_FREE_RATE_ANNUAL*100:.1f}% annual")
    print(f"  Min days to res:    {MIN_DAYS_TO_RESOLUTION}")
    print(f"  Cooldown:           {COOLDOWN_SECONDS}s")
    print(f"  Max staleness:      {MAX_STALENESS_MS}ms")
    print(f"  Min book levels:    {MIN_BOOK_LEVELS}")
    print(f"  Ledger:             {PAPER_LEDGER_PATH}")
    print(f"\n{'=' * 60}")
    print(f"Starting feeds... (Ctrl+C to stop)")
    print(f"{'=' * 60}\n")

    await asyncio.gather(
        polymarket_stream(manager, poly_token_ids),
        kalshi_stream(manager, kalshi_tickers),
        status_printer(manager, interval=30),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n  Engine stopped by user.")
        print(f"  Check {PAPER_LEDGER_PATH} for paper trades.\n")