"""Mapping pipeline for resolved markets (spec section 2).

Input: the loader checkpoints (data/checkpoints/kalshi/markets.jsonl and
data/checkpoints/polymarket/markets.jsonl). Output: data/candidate_pairs.csv with one row per
Kalshi market that passed every gate, including the reference token, the alignment method, the
deterministic check summary, and the LLM verdict. Rejections go to data/rejected_pairs.csv with
the rules-based reason (never a price-based one).

Gates, in order (each is a root-cause guard, none is event-specific):
1. candidate generation: token overlap between the Polymarket question and the Kalshi title
   plus yes_sub_title, and end dates within 45 days;
2. side alignment RESOLVED deterministically (sides.py);
3. resolution checks with no FAIL (resolution.py);
4. LLM verdict MATCH (llm_verify.py) for every pair, whether or not the deterministic checks
   left UNKNOWNs, because a deterministic PASS on text heuristics is not identity;
5. one-to-one: a Kalshi market maps to at most one Polymarket market and vice versa; ties are
   rejected, not resolved by score.

Usage: .venv/bin/python -m strategies.ref_mm.mapping.pipeline [--offline] [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from datetime import timedelta
from typing import Any

from pmcore.data.checkpoint import checkpoint_dir, read_jsonl
from pmcore.data.holdout import repo_root
from pmcore.data.kalshi_markets import iter_markets
from pmcore.venues.kalshi.normalize import parse_ts
from pmcore.venues.polymarket.normalize import GammaMarket, NormalizeError
from strategies.ref_mm.mapping import llm_verify, resolution, sides
from strategies.ref_mm.mapping.text import content_tokens, jaccard

CANDIDATE_MIN_JACCARD = 0.25
DATE_WINDOW = timedelta(days=45)


def _gamma_from_row(r: dict[str, Any]) -> GammaMarket:
    return GammaMarket(
        market_id=str(r["market_id"]),
        condition_id=str(r["condition_id"]),
        question=str(r["question"]),
        outcomes=tuple(json.loads(r["outcomes"])),
        token_ids=tuple(json.loads(r["token_ids"])),
        description=str(r.get("description") or ""),
        end_date=r.get("end_date"),
        closed=bool(r.get("closed")),
        event_id=r.get("event_id"),
        raw=str(r.get("raw") or ""),
    )


def generate_candidates(
    kalshi: list[dict[str, Any]], poly: list[GammaMarket]
) -> list[tuple[dict[str, Any], GammaMarket, float]]:
    """Cheap blocking by end-date proximity, then token overlap."""
    by_day: dict[Any, list[GammaMarket]] = defaultdict(list)
    ptoks: dict[str, set[str]] = {}
    for p in poly:
        if not p.end_date:
            continue
        try:
            d = parse_ts(p.end_date).date()
        except ValueError:
            continue
        by_day[d].append(p)
        ptoks[p.market_id] = content_tokens(p.question)
    out = []
    for k in kalshi:
        close = k.get("close_time") or k.get("expiration_time")
        if not close:
            continue
        kd = parse_ts(close).date()
        ktoks = content_tokens(k.get("title") or "") | content_tokens(k.get("yes_sub_title") or "")
        if not ktoks:
            continue
        for off in range(-DATE_WINDOW.days, DATE_WINDOW.days + 1):
            for p in by_day.get(kd + timedelta(days=off), ()):
                s = jaccard(ktoks, ptoks[p.market_id])
                if s >= CANDIDATE_MIN_JACCARD:
                    out.append((k, p, s))
    return out


def evaluate_candidate(
    k: dict[str, Any], p: GammaMarket, score: float, *, offline: bool
) -> dict[str, Any]:
    pair_id = f"{k['ticker']}|{p.market_id}"
    base = {
        "pair_id": pair_id,
        "kalshi_ticker": k["ticker"],
        "kalshi_event_ticker": k.get("event_ticker") or "",
        "kalshi_category": k.get("category") or "",
        "kalshi_title": k.get("title") or "",
        "kalshi_yes_sub_title": k.get("yes_sub_title") or "",
        "kalshi_rules_primary": k.get("rules_primary") or "",
        "kalshi_rules_secondary": k.get("rules_secondary") or "",
        "kalshi_close_time": k.get("close_time") or "",
        "kalshi_expiration_time": k.get("expiration_time") or "",
        "kalshi_result": k.get("result") or "",
        "poly_market_id": p.market_id,
        "condition_id": p.condition_id,
        "poly_question": p.question,
        "poly_outcomes": json.dumps(list(p.outcomes)),
        "poly_token_ids": json.dumps(list(p.token_ids)),
        "poly_description": p.description,
        "poly_end_date": p.end_date or "",
        "candidate_score": f"{score:.3f}",
    }
    try:
        sa = sides.align_sides(
            p, base["kalshi_title"], base["kalshi_yes_sub_title"], base["kalshi_rules_primary"]
        )
    except NormalizeError as e:
        sa = sides.SideAlignment("REJECT", None, None, "error", str(e))
    base.update(
        side_status=sa.status,
        side_method=sa.method,
        side_reason=sa.reason,
        reference_token_id=sa.reference_token_id or "",
        reference_outcome=sa.reference_outcome or "",
    )
    if sa.status != "RESOLVED":
        return {**base, "decision": "REJECT", "reason": f"side alignment {sa.status}: {sa.reason}"}
    rep = resolution.evaluate(
        p.question,
        p.description,
        p.end_date,
        {
            "title": base["kalshi_title"],
            "yes_sub_title": base["kalshi_yes_sub_title"],
            "rules_primary": base["kalshi_rules_primary"],
            "rules_secondary": base["kalshi_rules_secondary"],
            "close_time": k.get("close_time"),
            "expiration_time": k.get("expiration_time"),
        },
    )
    base["checks_summary"] = rep.summary()
    if rep.any_fail:
        return {
            **base,
            "decision": "REJECT",
            "reason": f"deterministic check failed: {rep.summary()}",
        }
    res = llm_verify.verify_pair(base, deterministic_side="same", offline=offline)
    base.update(
        llm_final=res.final, llm_cached=res.cached, llm_note=res.error, llm_cache_key=res.cache_key
    )
    if res.final != "MATCH":
        return {**base, "decision": "REJECT", "reason": f"LLM: {res.error}"}
    return {
        **base,
        "decision": "ACCEPT",
        "reason": "side resolved; deterministic checks no FAIL; LLM MATCH on all dimensions",
    }


def enforce_one_to_one(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    accepted = [r for r in rows if r["decision"] == "ACCEPT"]
    by_k: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_p: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in accepted:
        by_k[r["kalshi_ticker"]].append(r)
        by_p[r["poly_market_id"]].append(r)
    for r in accepted:
        if len(by_k[r["kalshi_ticker"]]) > 1 or len(by_p[r["poly_market_id"]]) > 1:
            r["decision"] = "REJECT"
            r["reason"] = "not one-to-one: multiple accepted counterparties (rules-based ambiguity)"
    return rows


def stratified_sample(
    accepted: list[dict[str, Any]], n: int = 100, seed: int = 20240101
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in accepted:
        by_cat[r["kalshi_category"] or "unknown"].append(r)
    total = len(accepted)
    out: list[dict[str, Any]] = []
    for _cat, rows in sorted(by_cat.items()):
        k = max(1, round(n * len(rows) / total)) if total else 0
        out.extend(rng.sample(rows, min(k, len(rows))))
    remaining = [r for r in accepted if r not in out]
    while len(out) < min(n, total) and remaining:
        out.append(remaining.pop(rng.randrange(len(remaining))))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="use cached LLM verdicts only")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    root = repo_root()
    kalshi = iter_markets()
    poly = [_gamma_from_row(r) for r in read_jsonl(checkpoint_dir("polymarket") / "markets.jsonl")]
    cands = generate_candidates(kalshi, poly)
    if args.limit:
        cands = cands[: args.limit]
    rows = [evaluate_candidate(k, p, s, offline=args.offline) for k, p, s in cands]
    rows = enforce_one_to_one(rows)
    fields = sorted({key for r in rows for key in r})
    (root / "data").mkdir(exist_ok=True)
    acc = [r for r in rows if r["decision"] == "ACCEPT"]
    rej = [r for r in rows if r["decision"] != "ACCEPT"]
    for name, subset in (("candidate_pairs.csv", acc), ("rejected_pairs.csv", rej)):
        with (root / "data" / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(subset)
    sample = stratified_sample(acc)
    with (root / "data" / "review_sample.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(sample)
    print(
        json.dumps(
            {
                "candidates": len(cands),
                "accepted": len(acc),
                "rejected": len(rej),
                "review_sample": len(sample),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
