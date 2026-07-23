# ---- strategy2_resolver.py — Sports Head-to-Head Matcher ----
# Category: Sports (events containing "vs" in title)

import pandas as pd
import requests
import json
import re
import time
from collections import Counter
from config import (
    KALSHI_REST_BASE,
    POLYMARKET_GAMMA_BASE,
    CONFIRMED_MATCHES_PATH,
)

OUTPUT_PATH = "data/strategy2_pairs.csv"
ERRORS_PATH = "data/strategy2_errors.csv"


# ---- API FETCHERS ----

def fetch_polymarket_event(event_id):
    url = f"{POLYMARKET_GAMMA_BASE}/events/{event_id}"
    resp = requests.get(url, timeout=15)
    if resp.status_code == 404:
        url = f"{POLYMARKET_GAMMA_BASE}/events"
        resp = requests.get(url, params={"id": event_id}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        raise ValueError(f"No event found for ID {event_id}")
    resp.raise_for_status()
    return resp.json()


def fetch_kalshi_event(event_ticker):
    url = f"{KALSHI_REST_BASE}/events/{event_ticker}"
    resp = requests.get(url, params={"with_nested_markets": "true"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("event", data)


# ---- MARKET PARSERS (with h2h-specific filters) ----

# Questions that indicate non-winner contract types
NON_WINNER_PATTERNS = [
    r"^Spread:",
    r"^Over ",
    r"^Under ",
    r"Total ",
    r"More Markets",
    r"Lower Strikes",
    r"Toss Match",
    r"Handicap",
    r" - More Markets$",
    # Draw contracts resolve on NEITHER team winning — structurally
    r"end in a draw",
    r"Draw at halftime",
    # "Leading at halftime" is NOT filtered — it IS equivalent to
    # "First Half Winner" for the named team.
]

def is_non_winner_question(question):
    """Check if a question is a spread/total/draw/other non-winner contract."""
    for pat in NON_WINNER_PATTERNS:
        if re.search(pat, question, re.IGNORECASE):
            return True
    return False


def parse_polymarket_markets(event_data):
    markets = event_data.get("markets", [])
    parsed = []
    for m in markets:
        if m.get("closed", False) and not m.get("active", False):
            continue

        question = m.get("question", "")
        if is_non_winner_question(question):
            continue

        token_ids_raw = m.get("clobTokenIds", "[]")
        try:
            token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
        except (json.JSONDecodeError, TypeError):
            token_ids = []
        if not token_ids or len(token_ids) < 2:
            continue

        outcomes_raw = m.get("outcomes", '["Yes", "No"]')
        try:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        except (json.JSONDecodeError, TypeError):
            outcomes = ["Yes", "No"]

        parsed.append({
            "question": question,
            "yes_token_id": str(token_ids[0]),
            "no_token_id": str(token_ids[1]),
            "outcomes": outcomes,
            "condition_id": m.get("conditionId", ""),
            "end_date": m.get("endDate", ""),
        })
    return parsed


def parse_kalshi_markets(event_data):
    markets = event_data.get("markets", [])
    parsed = []
    for m in markets:
        status = m.get("status", "")
        if status not in ("open", "unopened", "active"):
            continue
        parsed.append({
            "ticker": m.get("ticker", ""),
            "title": m.get("title", ""),
            "yes_sub_title": m.get("yes_sub_title", ""),
            "no_sub_title": m.get("no_sub_title", ""),
            "subtitle": m.get("subtitle", ""),
            "status": status,
            "expiration_time": m.get("expiration_time", ""),
        })
    return parsed


# ---- ENTITY EXTRACTION (same core as Strategy 1) ----

STOP_WORDS = {
    "will", "the", "be", "in", "of", "for", "a", "an", "to", "on",
    "at", "by", "or", "and", "is", "this", "that", "who", "what",
    "win", "winner", "won", "vs", "beat", "defeat", "game", "match",
    "2024", "2025", "2026", "2027", "2028", "2029", "2030",
}


def normalize_text(text):
    t = str(text).lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def tokenize(text):
    return normalize_text(text).split()


def extract_entities(titles):
    """Extract differentiating entity from each title within an event."""
    if not titles:
        return []

    if len(titles) == 1:
        tokens = tokenize(titles[0])
        entity_tokens = [t for t in tokens if t not in STOP_WORDS and not t.isdigit()]
        return [" ".join(entity_tokens)]

    token_lists = [tokenize(t) for t in titles]

    token_doc_freq = Counter()
    for tokens in token_lists:
        for t in set(tokens):
            token_doc_freq[t] += 1

    threshold = len(titles)
    scaffolding = {t for t, count in token_doc_freq.items() if count >= threshold}
    scaffolding.update(STOP_WORDS)

    entities = []
    for tokens in token_lists:
        entity_tokens = [t for t in tokens if t not in scaffolding]
        entities.append(" ".join(entity_tokens))

    return entities


def extract_kalshi_entity_fallback(ticker):
    parts = ticker.split("-")
    if len(parts) >= 3:
        return parts[-1].lower()
    return ""


# ---- ENTITY MATCHING (same fixed logic as Strategy 1) ----

def compute_entity_score(p_entity, k_entity):
    """Compute match score between two entity strings."""
    if p_entity == k_entity:
        return 1.0, "exact"

    p_tokens = set(p_entity.split())
    k_tokens = set(k_entity.split())

    # Single-token: exact only
    if len(p_tokens) < 2 or len(k_tokens) < 2:
        return 0.0, "no_match"

    # Jaccard
    intersection = p_tokens & k_tokens
    union = p_tokens | k_tokens
    jaccard = len(intersection) / len(union) if union else 0.0

    # Subset: shorter fully in longer
    shorter = p_tokens if len(p_tokens) <= len(k_tokens) else k_tokens
    longer = k_tokens if len(p_tokens) <= len(k_tokens) else p_tokens
    subset_score = len(shorter & longer) / len(shorter) if shorter else 0.0

    score = max(jaccard, subset_score)
    return score, "token_overlap" if score > 0 else "no_match"


def match_entities(poly_entities, kalshi_entities):
    """Greedy best-score-first matching."""
    candidates = []
    for pi, p_entity in enumerate(poly_entities):
        if not p_entity.strip():
            continue
        for ki, k_entity in enumerate(kalshi_entities):
            if not k_entity.strip():
                continue
            score, _ = compute_entity_score(p_entity, k_entity)
            if score >= 0.50:
                candidates.append((pi, ki, score))

    candidates.sort(key=lambda x: -x[2])
    used_poly = set()
    used_kalshi = set()
    matches = []

    for pi, ki, score in candidates:
        if pi in used_poly or ki in used_kalshi:
            continue
        used_poly.add(pi)
        used_kalshi.add(ki)
        matches.append((pi, ki, score))

    return matches


# ---- POST-MATCH SEMANTIC POLARITY CHECK ----

# Detect when two questions have opposing semantic polarity: one side

INCLUSION_WORDS = [
    "played in", "held in", "hosted in", "hosted by", "takes place in",
    "remain", "remains", "stay", "stays", "stays with", "kept", "keeps",
    "added", "adds", "join", "joins", "joining", "added to",
    "approved", "confirmed", "enacted", "passed", "becomes",
    "elected", "nominated", "appointed",
    "launched", "released", "opens",
    "legalized", "allowed",
]

EXCLUSION_WORDS = [
    "relocated", "relocated away", "moved from", "moved out",
    "removed", "removed from", "canceled", "cancelled",
    "exits", "exit", "leaves", "leaving", "departs", "departure",
    "rejected", "blocked", "vetoed", "struck down",
    "out as", "ousted", "fired", "resigns", "resignation",
    "banned", "outlawed", "prohibited",
    "withdraws", "withdrawn",
]


def has_semantic_polarity_inversion(poly_q, kalshi_q):
    """
    Detect inclusion/exclusion language opposition between two questions.
    Returns True if the pair should be REJECTED (inverted polarity).
    """
    pq = poly_q.lower()
    kq = kalshi_q.lower()
    p_incl = any(w in pq for w in INCLUSION_WORDS)
    p_excl = any(w in pq for w in EXCLUSION_WORDS)
    k_incl = any(w in kq for w in INCLUSION_WORDS)
    k_excl = any(w in kq for w in EXCLUSION_WORDS)
    # Inverted: one side strictly inclusion, other strictly exclusion
    if (p_incl and not p_excl) and (k_excl and not k_incl):
        return True
    if (p_excl and not p_incl) and (k_incl and not k_excl):
        return True
    return False


def ticker_year_conflict(poly_q, kalshi_ticker):
    """Reject pairs where the Kalshi ticker year is incompatible with the Poly question year.

    Kalshi tickers encode a close year (SENATEOH-28 -> 2028). Close year is
    normally the event year or one year after it (season awards, YoY data).
    A 2+ year gap means a different election cycle or event entirely.
    """
    m = re.search(r"-(\d{2})(?=[A-Z-]|$)", str(kalshi_ticker))
    if not m:
        return False
    ticker_year = 2000 + int(m.group(1))
    current_year = time.gmtime().tm_year
    poly_years = [int(y) for y in re.findall(r"20[2-4]\d", str(poly_q))
                  if int(y) >= current_year]
    if not poly_years:
        return False
    for y in poly_years:
        if ticker_year - 1 <= y <= ticker_year + 1:
            return False
    return True


# ---- ALIGNMENT (Strategy 2 specific) ----

def align_markets(poly_markets, kalshi_markets):
    aligned = []

    # CASE 1: Direct binary (1:1)
    if len(poly_markets) == 1 and len(kalshi_markets) == 1:
        pm = poly_markets[0]
        km = kalshi_markets[0]
        # Semantic polarity check: inclusion vs exclusion
        if has_semantic_polarity_inversion(pm["question"], km["title"]):
            return aligned  # Empty — inverted semantic polarity
        aligned.append({
            "poly_yes_token": pm["yes_token_id"],
            "poly_no_token": pm["no_token_id"],
            "poly_question": pm["question"],
            "kalshi_ticker": km["ticker"],
            "kalshi_market_title": km["title"],
            "alignment_type": "direct_binary",
            "alignment_score": 1.0,
            "poly_entity": "",
            "kalshi_entity": "",
            "poly_end_date": pm.get("end_date", ""),
            "kalshi_expiration_time": km.get("expiration_time", "")
        })
        return aligned

    # CASE 2: Multi-market entity extraction
    poly_titles = [pm["question"] for pm in poly_markets]
    kalshi_titles = [km["title"] for km in kalshi_markets]

    poly_entities = extract_entities(poly_titles)
    kalshi_entities = extract_entities(kalshi_titles)

    # Fallback for identical Kalshi titles
    unique_kalshi = set(normalize_text(t) for t in kalshi_titles)
    if len(unique_kalshi) == 1 and len(kalshi_markets) > 1:
        kalshi_sub_titles = [km.get("yes_sub_title", "") for km in kalshi_markets]
        unique_subs = set(normalize_text(t) for t in kalshi_sub_titles if t)
        if len(unique_subs) > 1:
            kalshi_entities = extract_entities(kalshi_sub_titles)
        else:
            kalshi_entities = [
                extract_kalshi_entity_fallback(km["ticker"])
                for km in kalshi_markets
            ]

    entity_matches = match_entities(poly_entities, kalshi_entities)

    for pi, ki, score in entity_matches:
        pm = poly_markets[pi]
        km = kalshi_markets[ki]
        aligned.append({
            "poly_yes_token": pm["yes_token_id"],
            "poly_no_token": pm["no_token_id"],
            "poly_question": pm["question"],
            "kalshi_ticker": km["ticker"],
            "kalshi_market_title": km["title"],
            "alignment_type": "entity_match",
            "alignment_score": round(score, 3),
            "poly_entity": poly_entities[pi],
            "kalshi_entity": kalshi_entities[ki],
            "poly_end_date": pm.get("end_date", ""),
            "kalshi_expiration_time": km.get("expiration_time", "")
        })

    return aligned


# ---- EVENT-LEVEL DEDUPLICATION ----

def deduplicate_events(df):
    """
    Remove duplicate event-level matches before resolving:
    1. "More Markets" Poly events (already in poly_title)
    2. Reverse matchups (A vs B and B vs A → keep first)
    """
    before = len(df)

    # Remove "More Markets" at event level
    more_mask = df['poly_title'].str.contains('More Markets', na=False)
    df = df[~more_mask].copy()

    # Remove reverse matchups: same Kalshi ticker, different Poly events
    # Keep the first occurrence (arbitrary but consistent)
    df = df.drop_duplicates(subset='kalshi_event_ticker', keep='first')

    after = len(df)
    if before != after:
        print(f"  Event dedup: {before} → {after} events")

    return df.reset_index(drop=True)


def deduplicate_pairs(pairs_df):
    """Deduplicate at pair level: same Kalshi ticker + entity → keep best."""
    if len(pairs_df) == 0:
        return pairs_df

    before = len(pairs_df)
    pairs_df["dedup_key"] = pairs_df["kalshi_ticker"] + "|" + pairs_df["poly_entity"]
    pairs_df = pairs_df.sort_values("alignment_score", ascending=False)
    pairs_df = pairs_df.drop_duplicates(subset="dedup_key", keep="first")
    pairs_df = pairs_df.drop(columns=["dedup_key"])
    after = len(pairs_df)

    if before != after:
        print(f"    Pair dedup: {before} → {after} pairs")

    return pairs_df


# ---- MAIN ----

def main():
    print("=" * 60)
    print("STRATEGY 2: Sports Head-to-Head Matcher")
    print("=" * 60)

    df = pd.read_csv(CONFIRMED_MATCHES_PATH)
    sports = df[df["category"] == "Sports"].copy()

    # Filter to h2h events only (title contains "vs")
    h2h = sports[sports["poly_title"].str.contains(" vs", case=False, na=False)].copy()
    print(f"\n  Total sports events: {len(sports)}")
    print(f"  Head-to-head events: {len(h2h)}")

    # Event-level deduplication
    h2h = deduplicate_events(h2h)

    all_pairs = []
    errors = []
    skipped_no_markets = 0
    skipped_no_alignment = 0

    print(f"\n  Resolving...\n")

    for i, row in h2h.iterrows():
        poly_id = row["poly_event_id"]
        kalshi_ticker = row["kalshi_event_ticker"]
        idx = h2h.index.get_loc(i) + 1

        try:
            poly_event = fetch_polymarket_event(poly_id)
        except Exception as e:
            errors.append({
                "poly_event_id": poly_id,
                "kalshi_event_ticker": kalshi_ticker,
                "stage": "poly_fetch",
                "error": str(e)[:200],
            })
            time.sleep(0.5)
            continue
        time.sleep(0.15)

        try:
            kalshi_event = fetch_kalshi_event(kalshi_ticker)
        except Exception as e:
            errors.append({
                "poly_event_id": poly_id,
                "kalshi_event_ticker": kalshi_ticker,
                "stage": "kalshi_fetch",
                "error": str(e)[:200],
            })
            time.sleep(0.5)
            continue
        time.sleep(0.15)

        poly_markets = parse_polymarket_markets(poly_event)
        kalshi_markets = parse_kalshi_markets(kalshi_event)

        if not poly_markets or not kalshi_markets:
            skipped_no_markets += 1
            continue

        aligned = align_markets(poly_markets, kalshi_markets)

        aligned = [p for p in aligned
                   if not ticker_year_conflict(p["poly_question"], p["kalshi_ticker"])]
        if not aligned:
            skipped_no_alignment += 1
            errors.append({
                "poly_event_id": poly_id,
                "kalshi_event_ticker": kalshi_ticker,
                "stage": "alignment",
                "error": f"No matches (poly={len(poly_markets)}, kalshi={len(kalshi_markets)})",
            })
            continue

        for pair in aligned:
            pair["poly_event_id"] = poly_id
            pair["poly_event_title"] = row["poly_title"]
            pair["kalshi_event_ticker"] = kalshi_ticker
            pair["kalshi_event_title"] = row["kalshi_title"]
            pair["category"] = "Sports_H2H"
            all_pairs.append(pair)

        if idx % 25 == 0:
            print(f"  [{idx}/{len(h2h)}] pairs: {len(all_pairs)} | errors: {len(errors)}")

    # Post-alignment deduplication
    df_pairs = pd.DataFrame(all_pairs)
    if len(df_pairs) > 0:
        df_pairs = deduplicate_pairs(df_pairs)
        df_pairs.to_csv(OUTPUT_PATH, index=False)

    df_errors = pd.DataFrame(errors)
    if len(df_errors) > 0:
        df_errors.to_csv(ERRORS_PATH, index=False)

    # ── Report ──
    print(f"\n{'=' * 60}")
    print(f"STRATEGY 2 RESULTS")
    print(f"{'=' * 60}")
    print(f"  Input h2h events:          {len(h2h)}")
    print(f"  Resolved tradeable pairs:  {len(df_pairs)}")
    print(f"  Errors (API/fetch):        {len(df_errors)}")
    print(f"  Skipped (no active mkts):  {skipped_no_markets}")
    print(f"  Skipped (no entity match): {skipped_no_alignment}")

    if len(df_pairs) > 0:
        print(f"\n  Alignment type breakdown:")
        for atype, count in df_pairs["alignment_type"].value_counts().items():
            print(f"    {atype:<20} {count:>5}")

        print(f"\n  Alignment score stats:")
        print(f"    Mean:   {df_pairs['alignment_score'].mean():.3f}")
        print(f"    Min:    {df_pairs['alignment_score'].min():.3f}")
        print(f"    Median: {df_pairs['alignment_score'].median():.3f}")

        entity = df_pairs[df_pairs["alignment_type"] == "entity_match"]
        if len(entity) > 0:
            diff = entity[entity["poly_entity"] != entity["kalshi_entity"]]
            same = entity[entity["poly_entity"] == entity["kalshi_entity"]]
            print(f"\n  Exact entity string matches: {len(same)}")
            print(f"  Non-exact entity matches:    {len(diff)}")

            # Verify no draw questions slipped through
            draw_check = df_pairs[df_pairs["poly_question"].str.contains("draw|Draw", na=False)]
            if len(draw_check) > 0:
                print(f"\n  ⚠ WARNING: {len(draw_check)} draw questions in output!")
                for _, r in draw_check.iterrows():
                    print(f"    {r['poly_question'][:70]}")
            else:
                print(f"\n  ✓ Draw filter: 0 draw questions in output")

            if len(diff) > 0:
                print(f"\n  Non-exact matches (ALL — inspect these):")
                print(f"  {'-' * 55}")
                for _, r in diff.iterrows():
                    print(f"  [{r['alignment_score']:.3f}] \"{r['poly_entity']}\" → \"{r['kalshi_entity']}\"")
                    print(f"    Poly:   {str(r['poly_question'])[:65]}")
                    print(f"    Kalshi: {str(r['kalshi_market_title'])[:65]}")
                    print()

            print(f"\n  Exact match samples (10 random):")
            print(f"  {'-' * 55}")
            sample = same.sample(min(10, len(same)), random_state=42)
            for _, r in sample.iterrows():
                print(f"  [{r['alignment_score']:.3f}] \"{r['poly_entity']}\"")
                print(f"    Poly:   {str(r['poly_question'])[:65]}")
                print(f"    Kalshi: {str(r['kalshi_market_title'])[:65]}")
                print()

    print(f"\n  ✓ Pairs saved to {OUTPUT_PATH}")
    if len(df_errors) > 0:
        print(f"  ⚠ Errors saved to {ERRORS_PATH}")
    print()


if __name__ == "__main__":
    main()