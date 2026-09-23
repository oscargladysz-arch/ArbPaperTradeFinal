"""Normalize Polymarket Gamma markets and Data API trades. Read-only data (rule 10).

Sides: a Polymarket print is on one token. The mapper decides which token is the pair's YES
(`reference_token_id`) from outcome labels, never from index (ASSUMPTIONS P-API-4). Prints on
the other token are converted: price -> 1 - price and the taker direction flips
(PREREGISTRATION D1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pmcore.money import MAX_PRICE_TICKS, ticks_from_decimal_dollars

TakerDir = Literal["buy", "sell"]


class NormalizeError(ValueError):
    pass


def _json_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, str):
        try:
            out = json.loads(v)
        except json.JSONDecodeError as exc:
            raise NormalizeError(f"bad JSON list {v[:80]!r}") from exc
        return list(out) if isinstance(out, list) else []
    return list(v)


@dataclass(frozen=True, slots=True)
class GammaMarket:
    market_id: str
    condition_id: str
    question: str
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    description: str
    end_date: str | None
    closed: bool
    event_id: str | None
    raw: str

    def token_for_outcome(self, label: str) -> str:
        """Token id whose outcome label equals `label` (case-insensitive, stripped)."""
        want = label.strip().lower()
        hits = [
            t
            for o, t in zip(self.outcomes, self.token_ids, strict=True)
            if o.strip().lower() == want
        ]
        if len(hits) != 1:
            raise NormalizeError(
                f"outcome {label!r} matches {len(hits)} tokens on market {self.market_id}"
            )
        return hits[0]

    def as_row(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "question": self.question,
            "outcomes": json.dumps(list(self.outcomes), default=str),
            "token_ids": json.dumps(list(self.token_ids), default=str),
            "description": self.description,
            "end_date": self.end_date,
            "closed": self.closed,
            "event_id": self.event_id,
            "raw": self.raw,
        }


def parse_gamma_market(raw: dict[str, Any]) -> GammaMarket:
    outcomes = [str(o) for o in _json_list(raw.get("outcomes"))]
    tokens = [str(t) for t in _json_list(raw.get("clobTokenIds"))]
    if len(outcomes) != len(tokens):
        raise NormalizeError(f"outcomes/clobTokenIds length mismatch on market {raw.get('id')}")
    events = raw.get("events")
    event_id = None
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = str(events[0].get("id")) if events[0].get("id") is not None else None
    return GammaMarket(
        market_id=str(raw.get("id", "")),
        condition_id=str(raw.get("conditionId", "")),
        question=str(raw.get("question", "")),
        outcomes=tuple(outcomes),
        token_ids=tuple(tokens),
        description=str(raw.get("description", "") or ""),
        end_date=raw.get("endDate"),
        closed=bool(raw.get("closed", False)),
        event_id=event_id,
        raw=json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str),
    )


@dataclass(frozen=True, slots=True)
class PolyPrint:
    """One Polymarket fill expressed in terms of `stored_reference_token_id` (the token the
    caller passed as reference; in the lake this is clobTokenIds[0], a storage convention, NOT
    the pair's YES). `yes_price_ticks` and `taker_dir` are relative to that token. Use
    `in_pair_terms` to re-express a frame in a pair's reference token (audit findings C6, C7)."""

    condition_id: str
    ts: datetime
    taker_dir: TakerDir
    yes_price_ticks: int
    size_x100: int
    token_id: str
    on_reference_token: bool
    stored_reference_token_id: str
    tx_hash: str
    raw: str

    def as_row(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "ts_us": int(self.ts.timestamp() * 1_000_000),
            "date": self.ts.date().isoformat(),
            "taker_dir": self.taker_dir,
            "yes_price_ticks": self.yes_price_ticks,
            "size_x100": self.size_x100,
            "token_id": self.token_id,
            "on_reference_token": self.on_reference_token,
            "stored_reference_token_id": self.stored_reference_token_id,
            "tx_hash": self.tx_hash,
            "raw": self.raw,
        }


def normalize_data_api_trade(
    raw: dict[str, Any], reference_token_id: str, other_token_id: str
) -> PolyPrint:
    side = str(raw.get("side", "")).upper()
    if side not in ("BUY", "SELL"):
        raise NormalizeError(f"side must be BUY or SELL, got {raw.get('side')!r}")
    # P-API-2 (VERIFIED 2026-09-23): Data API v2 rows use token_id / transaction_hash / outcome_index;
    # v1 rows use asset / transactionHash. Prices and sizes arrive as JSON numbers, parsed as Decimal
    # by pmcore.venues.http.JsonClient.
    token = str(raw.get("token_id") or raw.get("asset") or "")
    price = ticks_from_decimal_dollars(str(raw["price"]))
    ts_raw = raw.get("timestamp")
    if isinstance(ts_raw, str) and ts_raw.isdigit():
        ts_raw = int(ts_raw)
    if isinstance(ts_raw, bool) or not isinstance(ts_raw, int | float):
        raise NormalizeError(f"bad timestamp {ts_raw!r}")
    ts = datetime.fromtimestamp(float(ts_raw), tz=UTC)
    taker: TakerDir = "buy" if side == "BUY" else "sell"
    if token == reference_token_id:
        yes_price = price
        on_ref = True
    elif token == other_token_id:
        yes_price = MAX_PRICE_TICKS - price
        taker = "sell" if taker == "buy" else "buy"
        on_ref = False
    else:
        raise NormalizeError(f"trade token {token} is neither reference nor other token")
    size = Decimal(str(raw.get("size", "0")))
    return PolyPrint(
        condition_id=str(raw.get("condition_id") or raw.get("conditionId") or ""),
        ts=ts,
        taker_dir=taker,
        yes_price_ticks=yes_price,
        size_x100=int(size * 100),
        token_id=token,
        on_reference_token=on_ref,
        stored_reference_token_id=reference_token_id,
        tx_hash=str(raw.get("transaction_hash") or raw.get("transactionHash") or ""),
        raw=json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str),
    )


def in_pair_terms(df: Any, reference_token_id: str) -> Any:
    """Re-express a prints DataFrame (polars) in the pair's reference token: rows stored under
    a different reference get price -> 10000 - price and buy/sell flipped. Raises if the frame
    mixes stored references or lacks the column, so an inversion can never be masked."""
    import polars as pl

    if "stored_reference_token_id" not in df.columns:
        raise NormalizeError(
            "prints frame lacks stored_reference_token_id; re-download with the current loader"
        )
    refs = df["stored_reference_token_id"].unique().to_list()
    if len(refs) != 1:
        raise NormalizeError(f"prints frame mixes stored references: {refs}")
    if refs[0] == reference_token_id:
        return df
    return df.with_columns(
        (MAX_PRICE_TICKS - pl.col("yes_price_ticks")).alias("yes_price_ticks"),
        pl.when(pl.col("taker_dir") == "buy")
        .then(pl.lit("sell"))
        .otherwise(pl.lit("buy"))
        .alias("taker_dir"),
        pl.lit(reference_token_id).alias("stored_reference_token_id"),
        (~pl.col("on_reference_token")).alias("on_reference_token"),
    )
