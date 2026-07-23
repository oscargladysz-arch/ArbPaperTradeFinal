# ---- strategy4_resolver.py — Numeric Bracket / Economics Matcher ----
# Categories: Economics, Climate and Weather

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

OUTPUT_PATH = "data/strategy4_pairs.csv"
ERRORS_PATH = "data/strategy4_errors.csv"
CATEGORIES = ["Economics", "Climate and Weather"]


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


# ---- MARKET PARSERS ----

def parse_polymarket_markets(event_data):
    markets = event_data.get("markets", [])
    parsed = []
    for m in markets:
        if m.get("closed", False) and not m.get("active", False):
            continue
        question = m.get("question", "")
        if "More Markets" in question or "Lower Strikes" in question:
            continue
        token_ids_raw = m.get("clobTokenIds", "[]")
        try:
            token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
        except (json.JSONDecodeError, TypeError):
            token_ids = []
        if not token_ids or len(token_ids) < 2:
            continue
        parsed.append({
            "question": question,
            "yes_token_id": str(token_ids[0]),
            "no_token_id": str(token_ids[1]),
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
            "subtitle": m.get("subtitle", ""),
            "expiration_time": m.get("expiration_time", ""),
        })
    return parsed


# ---- NUMERIC BRACKET DETECTION ----

# Patterns that indicate numeric range/threshold markets
NUMERIC_PATTERNS = [
    r"between\s+[\d.]+%?\s+and\s+[\d.]+%?",   # "between 1.5% and 2.0%"
    r"(more|greater|above|over|at least|exceed)\s+[\d.]+",  # "above 1.5%"
    r"(less|below|under|fewer)\s+than\s+[\d.]+",  # "less than 1.0%"
    r"[\d.]+ (or more|or higher|or above|\+)",    # "5% or higher"
    r"[\d.]+%?\s*[-–]\s*[\d.]+%?",               # "1.5%-2.0%" or "1.5–2.0"
    r"(between|be)\s+[\d.,]+m?\s*(and|&)\s*[\d.,]+m?",  # "between 14.2m & 14.4m"
]

# Patterns for extracting the exact numeric boundary from a question
BOUNDARY_EXTRACTORS = [
    # "between X and Y" → (X, Y)
    (r"between\s+([\d.]+)%?\s+and\s+([\d.]+)%?", lambda m: (float(m.group(1)), float(m.group(2)))),
    # "less than X" → (None, X)
    (r"less than\s+([\d.]+)", lambda m: (None, float(m.group(1)))),
    # "at least X" → (X, None)
    (r"at least\s+([\d.]+)", lambda m: (float(m.group(1)), None)),
    # "above X" or "more than X" → (X, None)
    (r"(?:above|more than|greater than|over|exceed)\s+([\d.]+)", lambda m: (float(m.group(1)), None)),
    # "below X" or "under X" → (None, X)
    (r"(?:below|under)\s+([\d.]+)", lambda m: (None, float(m.group(1)))),
    # "X or higher" → (X, None)
    (r"([\d.]+)%?\s+or\s+(?:higher|more|above)", lambda m: (float(m.group(1)), None)),
]


def is_numeric_question(question):
    """Check if a sub-market question involves numeric ranges/thresholds."""
    q = question.lower()
    for pat in NUMERIC_PATTERNS:
        if re.search(pat, q):
            return True
    return False


def extract_boundary(question):
    """
    Extract numeric boundary from a question.
    Returns (lower, upper) tuple. None means unbounded.
    Returns None if no boundary found.
    """
    q = question.lower()
    for pat, extractor in BOUNDARY_EXTRACTORS:
        match = re.search(pat, q)
        if match:
            return extractor(match)
    return None


def has_numeric_submarkets(markets):
    """Check if a list of sub-markets contains numeric range questions."""
    numeric_count = sum(1 for m in markets if is_numeric_question(m["question"] if "question" in m else m.get("title", "")))
    return numeric_count > len(markets) * 0.3  # More than 30% are numeric


# ---- ENTITY EXTRACTION (for non-numeric markets) ----

STOP_WORDS = {
    "will", "the", "be", "in", "of", "for", "a", "an", "to", "on",
    "at", "by", "or", "and", "is", "this", "that", "who", "what",
    "rate", "decision", "cut", "hike", "hold", "increase", "decrease",
    "bank", "reserve", "central", "meeting",
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


def extract_effective_year(text):
    """Extract effective deadline year from a market question."""
    t = text.lower()
    years = re.findall(r'(20[2-3]\d)', t)
    if not years:
        return None
    for year_str in years:
        yr = int(year_str)
        if re.search(r'(?:by|before)\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s+\d{1,2},?\s+' + year_str, t):
            return yr
        if re.search(r'(?:by|before)\s+' + year_str, t):
            return yr - 1
        if re.search(r'(?:in|during)\s+' + year_str, t):
            return yr
    return int(years[0])


def timeframes_compatible(poly_q, kalshi_q):
    """Check if two market questions have compatible timeframes."""
    p_eff = extract_effective_year(poly_q)
    k_eff = extract_effective_year(kalshi_q)
    if p_eff is None or k_eff is None:
        return True
    return p_eff == k_eff


def compute_entity_score(p_entity, k_entity):
    if p_entity == k_entity:
        return 1.0
    p_tokens = set(p_entity.split())
    k_tokens = set(k_entity.split())
    # Guard 1: if both entities are primarily numeric (>50% digit tokens),
    p_digit_frac = sum(1 for t in p_tokens if t.isdigit()) / len(p_tokens) if p_tokens else 0
    k_digit_frac = sum(1 for t in k_tokens if t.isdigit()) / len(k_tokens) if k_tokens else 0
    if p_digit_frac > 0.5 and k_digit_frac > 0.5:
        return 0.0  # Non-exact numeric entities → reject
    # Guard 2: if either entity contains a directional comparison word
    DIRECTIONAL_WORDS = {"less", "more", "greater", "above", "below",
                         "between", "least", "exceed", "under", "over", "than"}
    p_has_direction = bool(p_tokens & DIRECTIONAL_WORDS)
    k_has_direction = bool(k_tokens & DIRECTIONAL_WORDS)
    p_has_digits = any(t.isdigit() for t in p_tokens)
    k_has_digits = any(t.isdigit() for t in k_tokens)
    if (p_has_direction and p_has_digits) or (k_has_direction and k_has_digits):
        return 0.0  # Directional numeric entity → exact only, already failed
    if len(p_tokens) < 2 or len(k_tokens) < 2:
        return 0.0
    intersection = p_tokens & k_tokens
    union = p_tokens | k_tokens
    jaccard = len(intersection) / len(union) if union else 0.0
    shorter = p_tokens if len(p_tokens) <= len(k_tokens) else k_tokens
    longer = k_tokens if len(p_tokens) <= len(k_tokens) else p_tokens
    subset_score = len(shorter & longer) / len(shorter) if shorter else 0.0
    # Guard 3: bundled entity rejection
    matched = len(shorter & longer)
    unmatched = len(longer) - matched
    if matched > 0 and unmatched >= matched * 2:
        return 0.0
    return max(jaccard, subset_score)


def match_entities(poly_entities, kalshi_entities):
    candidates = []
    for pi, pe in enumerate(poly_entities):
        if not pe.strip():
            continue
        for ki, ke in enumerate(kalshi_entities):
            if not ke.strip():
                continue
            score = compute_entity_score(pe, ke)
            if score >= 0.50:
                candidates.append((pi, ki, score))
    candidates.sort(key=lambda x: -x[2])
    used_p, used_k = set(), set()
    matches = []
    for pi, ki, score in candidates:
        if pi in used_p or ki in used_k:
            continue
        used_p.add(pi)
        used_k.add(ki)
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


# ---- ALIGNMENT STRATEGIES ----

def align_numeric_markets(poly_markets, kalshi_markets):
    """
    Align numeric bracket markets by matching EXACT boundaries.
    Only pairs where both platforms have the same numeric boundary
    are considered matches. Different bracket structures are rejected.
    """
    aligned = []

    for pm in poly_markets:
        pq = pm["question"]
        p_boundary = extract_boundary(pq)
        if not p_boundary:
            continue

        for km in kalshi_markets:
            kq = km.get("title", "")
            k_boundary = extract_boundary(kq)
            if not k_boundary:
                continue

            # Exact boundary match
            if p_boundary == k_boundary:
                aligned.append({
                    "poly_yes_token": pm["yes_token_id"],
                    "poly_no_token": pm["no_token_id"],
                    "poly_question": pq,
                    "kalshi_ticker": km["ticker"],
                    "kalshi_market_title": kq,
                    "alignment_type": "exact_boundary",
                    "alignment_score": 1.0,
                    "poly_entity": str(p_boundary),
                    "kalshi_entity": str(k_boundary),
                    "poly_end_date": pm.get("end_date", ""),
                    "kalshi_expiration_time": km.get("expiration_time", ""),
                })
                break  # One-to-one matching

    return aligned


def align_entity_markets(poly_markets, kalshi_markets):
    """Standard entity extraction alignment for non-numeric markets."""
    aligned = []

    if len(poly_markets) == 1 and len(kalshi_markets) == 1:
        pm = poly_markets[0]
        km = kalshi_markets[0]
        # Timeframe guard: reject if effective deadline years differ
        if not timeframes_compatible(pm["question"], km["title"]):
            return aligned  # Empty — timeframe mismatch
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
            "kalshi_expiration_time": km.get("expiration_time", ""),
        })
        return aligned

    poly_titles = [pm["question"] for pm in poly_markets]
    kalshi_titles = [km["title"] for km in kalshi_markets]

    poly_entities = extract_entities(poly_titles)
    kalshi_entities = extract_entities(kalshi_titles)

    unique_kalshi = set(normalize_text(t) for t in kalshi_titles)
    if len(unique_kalshi) == 1 and len(kalshi_markets) > 1:
        kalshi_sub_titles = [km.get("yes_sub_title", "") for km in kalshi_markets]
        unique_subs = set(normalize_text(t) for t in kalshi_sub_titles if t)
        if len(unique_subs) > 1:
            kalshi_entities = extract_entities(kalshi_sub_titles)
        else:
            kalshi_entities = [km["ticker"].split("-")[-1].lower() for km in kalshi_markets]

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
            "kalshi_expiration_time": km.get("expiration_time", ""),
        })

    return aligned


# ---- DEDUPLICATION ----

def deduplicate_events(df):
    before = len(df)
    more_mask = df['poly_title'].str.contains('More Markets', na=False)
    df = df[~more_mask].copy()

    # Remove GDP country mismatches (Kalshi KXGDPYEAR-26 is US-only)
    gdp_rows = df[df['kalshi_event_ticker'] == 'KXGDPYEAR-26']
    non_us = gdp_rows[
        gdp_rows['poly_title'].str.contains('China', na=False) |
        gdp_rows['poly_title'].str.contains('UK', na=False) |
        gdp_rows['poly_title'].str.contains('World', na=False)
    ]
    if len(non_us) > 0:
        df = df.drop(non_us.index)
        print(f"  Removed {len(non_us)} GDP country mismatches")

    df = df.drop_duplicates(subset='kalshi_event_ticker', keep='first')
    after = len(df)
    if before != after:
        print(f"  Event dedup: {before} → {after}")
    return df.reset_index(drop=True)


def deduplicate_pairs(pairs_df):
    if len(pairs_df) == 0:
        return pairs_df
    before = len(pairs_df)
    pairs_df["dedup_key"] = pairs_df["kalshi_ticker"] + "|" + pairs_df["poly_entity"]
    pairs_df = pairs_df.sort_values("alignment_score", ascending=False)
    pairs_df = pairs_df.drop_duplicates(subset="dedup_key", keep="first")
    pairs_df = pairs_df.drop(columns=["dedup_key"])
    after = len(pairs_df)
    if before != after:
        print(f"    Pair dedup: {before} → {after}")
    return pairs_df


# ---- MAIN ----

def main():
    print("=" * 60)
    print("STRATEGY 4: Numeric Bracket / Economics Matcher")
    print(f"Categories: {', '.join(CATEGORIES)}")
    print("=" * 60)

    df = pd.read_csv(CONFIRMED_MATCHES_PATH)
    df = df[df["category"].isin(CATEGORIES)].copy()
    print(f"\n  Events to resolve: {len(df)}")

    df = deduplicate_events(df)

    all_pairs = []
    errors = []
    skipped_no_markets = 0
    skipped_no_alignment = 0
    numeric_events = 0
    entity_events = 0

    print(f"\n  Resolving...\n")

    for i, row in df.iterrows():
        poly_id = row["poly_event_id"]
        kalshi_ticker = row["kalshi_event_ticker"]
        idx = df.index.get_loc(i) + 1

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

        # Route to appropriate alignment strategy
        poly_is_numeric = has_numeric_submarkets(poly_markets)
        kalshi_is_numeric = has_numeric_submarkets(kalshi_markets)

        if poly_is_numeric and kalshi_is_numeric:
            # Both sides numeric → exact boundary matching only, no fallback
            numeric_events += 1
            aligned = align_numeric_markets(poly_markets, kalshi_markets)
        else:
            # One or neither side numeric → entity matching
            entity_events += 1
            aligned = align_entity_markets(poly_markets, kalshi_markets)

        aligned = [p for p in aligned
                   if not ticker_year_conflict(p["poly_question"], p["kalshi_ticker"])]
        if not aligned:
            skipped_no_alignment += 1
            errors.append({
                "poly_event_id": poly_id,
                "kalshi_event_ticker": kalshi_ticker,
                "stage": "alignment",
                "error": f"No matches (poly={len(poly_markets)}, kalshi={len(kalshi_markets)}, both_numeric={poly_is_numeric and kalshi_is_numeric})",
            })
            continue

        for pair in aligned:
            pair["poly_event_id"] = poly_id
            pair["poly_event_title"] = row["poly_title"]
            pair["kalshi_event_ticker"] = kalshi_ticker
            pair["kalshi_event_title"] = row["kalshi_title"]
            pair["category"] = row["category"]
            all_pairs.append(pair)

    df_pairs = pd.DataFrame(all_pairs)
    if len(df_pairs) > 0:
        df_pairs = deduplicate_pairs(df_pairs)
        df_pairs.to_csv(OUTPUT_PATH, index=False)

    df_errors = pd.DataFrame(errors)
    if len(df_errors) > 0:
        df_errors.to_csv(ERRORS_PATH, index=False)

    # Report
    print(f"\n{'=' * 60}")
    print(f"STRATEGY 4 RESULTS")
    print(f"{'=' * 60}")
    print(f"  Input events:              {len(df)}")
    print(f"  Numeric bracket events:    {numeric_events}")
    print(f"  Entity-based events:       {entity_events}")
    print(f"  Resolved tradeable pairs:  {len(df_pairs)}")
    print(f"  Errors (API/fetch):        {len(df_errors)}")
    print(f"  Skipped (no active mkts):  {skipped_no_markets}")
    print(f"  Skipped (no alignment):    {skipped_no_alignment}")

    if len(df_pairs) > 0:
        print(f"\n  Alignment type breakdown:")
        for atype, count in df_pairs["alignment_type"].value_counts().items():
            print(f"    {atype:<20} {count:>5}")

        print(f"\n  All resolved pairs:")
        print(f"  {'-' * 55}")
        for _, r in df_pairs.iterrows():
            print(f"  [{r['alignment_type']}] score={r['alignment_score']}")
            print(f"    Poly:   {str(r['poly_question'])[:65]}")
            print(f"    Kalshi: {str(r['kalshi_market_title'])[:65]}")
            print(f"    Entity: \"{r['poly_entity']}\" → \"{r['kalshi_entity']}\"")
            print()

    print(f"\n  ✓ Pairs saved to {OUTPUT_PATH}")
    if len(df_errors) > 0:
        print(f"  ⚠ Errors saved to {ERRORS_PATH}")
    print()


if __name__ == "__main__":
    main()