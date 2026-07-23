# ---- strategy1_resolver.py — Named Entity Binary Matcher ----
# Categories: Elections, Politics, Entertainment, Mentions

import pandas as pd
import requests
import json
import re
import time
import sys
from collections import Counter
from config import (
    KALSHI_REST_BASE,
    POLYMARKET_GAMMA_BASE,
    CONFIRMED_MATCHES_PATH,
)

OUTPUT_PATH = "data/strategy1_pairs.csv"
ERRORS_PATH = "data/strategy1_errors.csv"
CATEGORIES = ["Elections", "Politics", "Entertainment", "Mentions"]


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

        # Pre-filter: different contract types
        if question.startswith("Spread:"):
            continue
        if "More Markets" in question:
            continue
        if "Lower Strikes" in question or "Lower strikes" in question:
            continue
        if "Toss Match Double" in question:
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


# ---- ENTITY EXTRACTION ----

STOP_WORDS = {
    "will", "the", "be", "in", "of", "for", "a", "an", "to", "on",
    "at", "by", "or", "and", "is", "this", "that", "who", "what",
    "win", "winner", "won", "finish", "before", "after", "during",
    "next", "new", "first", "second", "third", "year", "become",
    "law", "act", "bill", "vote", "confirmed", "nominee", "nominated",
    "republican", "democratic", "democrat", "party", "primary",
    "election", "race", "seat", "seats", "house", "senate", "governor",
    "presidential", "mayoral", "parliamentary",
    "2024", "2025", "2026", "2027", "2028", "2029", "2030",
}

# Anonymized / placeholder patterns on Polymarket
ANON_PATTERNS = [
    r"^person\s+[a-z0-9]{1,3}$",
    r"^candidate\s+[a-z0-9]{1,3}$",
    r"^player\s+[a-z0-9]{1,3}$",
    r"^artist\s+[a-z0-9]{1,3}$",
    r"^team\s+[a-z0-9]{1,3}$",
    r"^placeholder\s+[a-z0-9]{1,3}$",
    r"^another\s+candidate$",
    r"^another\s+player$",
    r"^other$",
]


def normalize_text(text):
    t = str(text).lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def tokenize(text):
    return normalize_text(text).split()


def is_anonymized(entity_str):
    """Check if entity is a Polymarket placeholder name."""
    e = entity_str.strip().lower()
    for pattern in ANON_PATTERNS:
        if re.match(pattern, e):
            return True
    return False


def extract_entities(titles):
    """
    Given sub-market titles from ONE side of ONE event,
    extract the differentiating entity from each.

    Tokens appearing in ALL titles = scaffolding (shared structure).
    What remains per title = the entity.
    """
    if not titles:
        return []

    if len(titles) == 1:
        tokens = tokenize(titles[0])
        entity_tokens = [t for t in tokens if t not in STOP_WORDS and not t.isdigit()]
        return [" ".join(entity_tokens)]

    token_lists = [tokenize(t) for t in titles]

    # Scaffolding = tokens appearing in ALL titles
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
    """Extract entity hint from Kalshi ticker suffix as last resort."""
    parts = ticker.split("-")
    if len(parts) >= 3:
        return parts[-1].lower()
    return ""


# ---- ENTITY MATCHING ----

# Directional / comparison words that are structurally meaningful
DIRECTIONAL_WORDS = {
    "less", "more", "greater", "above", "below",
    "between", "least", "exceed", "under", "over", "than",
}


def compute_entity_score(p_entity, k_entity):
    """
    Compute match score between two entity strings.

    Returns (score, match_type) where:
      - score: 0.0 to 1.0
      - match_type: 'exact', 'token_overlap', or 'no_match'

    Guards (ported from Strategy 4 + new digit-divergence guard):
      1. Digit-heavy entities (>50% digit tokens) → exact only
      2. Directional words + digits → exact only
      3. Same non-digit tokens, different digits → reject
      4. Bundled entity rejection (unmatched >> matched)
    """
    if p_entity == k_entity:
        return 1.0, "exact"

    p_tokens = set(p_entity.split())
    k_tokens = set(k_entity.split())

    # ── Guard 1: digit-heavy entities → exact only ──
    p_digit_frac = sum(1 for t in p_tokens if t.isdigit()) / len(p_tokens) if p_tokens else 0
    k_digit_frac = sum(1 for t in k_tokens if t.isdigit()) / len(k_tokens) if k_tokens else 0
    if p_digit_frac > 0.5 and k_digit_frac > 0.5:
        return 0.0, "no_match"

    # ── Guard 2: directional words + digits → exact only ──
    p_has_direction = bool(p_tokens & DIRECTIONAL_WORDS)
    k_has_direction = bool(k_tokens & DIRECTIONAL_WORDS)
    p_has_digits = any(t.isdigit() for t in p_tokens)
    k_has_digits = any(t.isdigit() for t in k_tokens)
    if (p_has_direction and p_has_digits) or (k_has_direction and k_has_digits):
        return 0.0, "no_match"

    # ── Guard 3: digit-divergence on shared-name entities ──
    p_digits = {t for t in p_tokens if t.isdigit()}
    k_digits = {t for t in k_tokens if t.isdigit()}
    p_non_digits = p_tokens - p_digits
    k_non_digits = k_tokens - k_digits
    if p_non_digits == k_non_digits and p_digits and k_digits and p_digits != k_digits:
        return 0.0, "no_match"

    # Single-token entities: require exact match only
    # This prevents "boston" ≈ "boston" across different teams/contexts
    if len(p_tokens) < 2 or len(k_tokens) < 2:
        return 0.0, "no_match"

    # Jaccard similarity: |intersection| / |union|
    intersection = p_tokens & k_tokens
    union = p_tokens | k_tokens
    jaccard = len(intersection) / len(union) if union else 0.0

    # Subset check: are all tokens from the shorter entity
    # contained in the LONGER entity? (not the union!)
    shorter = p_tokens if len(p_tokens) <= len(k_tokens) else k_tokens
    longer = k_tokens if len(p_tokens) <= len(k_tokens) else p_tokens
    subset_score = len(shorter & longer) / len(shorter) if shorter else 0.0

    # ── Guard 4: bundled entity rejection ──
    if len(shorter) >= 3:
        matched = len(shorter & longer)
        unmatched = len(longer) - matched
        if matched > 0 and unmatched >= matched * 2:
            return 0.0, "no_match"

    score = max(jaccard, subset_score)
    return score, "token_overlap" if score > 0 else "no_match"


def match_entities(poly_entities, kalshi_entities):
    """
    Match extracted entity strings across platforms.

    Each Poly entity is matched to at most one Kalshi entity.
    Each Kalshi entity is matched to at most one Poly entity.
    Greedy: best score first, then lock both sides.
    """
    # Build all candidate pairs with scores
    candidates = []
    for pi, p_entity in enumerate(poly_entities):
        if not p_entity.strip():
            continue
        if is_anonymized(p_entity):
            continue

        for ki, k_entity in enumerate(kalshi_entities):
            if not k_entity.strip():
                continue

            score, match_type = compute_entity_score(p_entity, k_entity)
            if score >= 0.50:
                candidates.append((pi, ki, score, match_type))

    # Sort by score descending, then greedily assign
    candidates.sort(key=lambda x: -x[2])
    used_poly = set()
    used_kalshi = set()
    matches = []

    for pi, ki, score, match_type in candidates:
        if pi in used_poly or ki in used_kalshi:
            continue
        used_poly.add(pi)
        used_kalshi.add(ki)
        matches.append((pi, ki, score))

    return matches


# ---- POST-MATCH POLARITY CHECK ----

# Detect when two questions have opposite directional operators

LESS_INDICATORS = ["less than", "below", "under", "fewer than"]
MORE_INDICATORS = ["above", "more than", "greater than", "over", "at least", "exceed"]


def has_opposing_direction(poly_q, kalshi_q, poly_entity, kalshi_entity):
    """
    Detect inverted bracket polarity between two matched questions.
    Only applies when entities contain digits (numeric bracket pairs).
    Returns True if the pair should be REJECTED.
    """
    # Only check when entities contain digits
    if not re.search(r"\d", str(poly_entity)) and not re.search(r"\d", str(kalshi_entity)):
        return False

    pq = poly_q.lower()
    kq = kalshi_q.lower()

    p_has_less = any(w in pq for w in LESS_INDICATORS) or "<" in pq
    p_has_more = any(w in pq for w in MORE_INDICATORS) or ">" in pq
    k_has_less = any(w in kq for w in LESS_INDICATORS) or "<" in kq
    k_has_more = any(w in kq for w in MORE_INDICATORS) or ">" in kq

    # Opposing: one side is strictly less, the other strictly more
    if (p_has_less and not p_has_more) and (k_has_more and not k_has_less):
        return True
    if (p_has_more and not p_has_less) and (k_has_less and not k_has_more):
        return True

    return False


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


# ---- ALIGNMENT ----

def align_markets(poly_markets, kalshi_markets):
    """Align sub-markets using entity extraction."""
    aligned = []

    # CASE 1: Direct binary (1:1)
    if len(poly_markets) == 1 and len(kalshi_markets) == 1:
        pm = poly_markets[0]
        km = kalshi_markets[0]
        # Polarity check even for direct binary
        if has_opposing_direction(pm["question"], km["title"], "", ""):
            return aligned  # Empty — inverted polarity
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

    # CASE 2: Multi-market → entity extraction
    poly_titles = [pm["question"] for pm in poly_markets]
    kalshi_titles = [km["title"] for km in kalshi_markets]

    poly_entities = extract_entities(poly_titles)
    kalshi_entities = extract_entities(kalshi_titles)

    # Fallback: if all Kalshi titles identical, try yes_sub_title or ticker
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

    # Match
    entity_matches = match_entities(poly_entities, kalshi_entities)

    for pi, ki, score in entity_matches:
        pm = poly_markets[pi]
        km = kalshi_markets[ki]
        # Polarity check: reject "<70 seats" vs "above 70 seats"
        if has_opposing_direction(
            pm["question"], km["title"],
            poly_entities[pi], kalshi_entities[ki]
        ):
            continue
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


# ---- POST-ALIGNMENT DEDUPLICATION ----

def deduplicate_pairs(pairs_df):
    """
    If same Kalshi ticker + entity appears multiple times
    (from different Poly events), keep highest alignment score.
    """
    if len(pairs_df) == 0:
        return pairs_df

    before = len(pairs_df)
    pairs_df["dedup_key"] = pairs_df["kalshi_ticker"] + "|" + pairs_df["poly_entity"]
    pairs_df = pairs_df.sort_values("alignment_score", ascending=False)
    pairs_df = pairs_df.drop_duplicates(subset="dedup_key", keep="first")
    pairs_df = pairs_df.drop(columns=["dedup_key"])
    after = len(pairs_df)

    if before != after:
        print(f"    Deduplication: {before} → {after} pairs")

    return pairs_df


# ---- MAIN ----

def main():
    print("=" * 60)
    print("STRATEGY 1: Named Entity Binary Matcher")
    print(f"Categories: {', '.join(CATEGORIES)}")
    print("=" * 60)

    df = pd.read_csv(CONFIRMED_MATCHES_PATH)
    df = df[df["category"].isin(CATEGORIES)].copy()
    print(f"\n  Events to resolve: {len(df)}")
    for cat, count in df["category"].value_counts().items():
        print(f"    {cat:<25} {count:>5}")

    all_pairs = []
    errors = []
    skipped_no_markets = 0
    skipped_no_alignment = 0

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
            pair["category"] = row["category"]
            all_pairs.append(pair)

        if idx % 50 == 0:
            print(f"  [{idx}/{len(df)}] pairs: {len(all_pairs)} | errors: {len(errors)}")

        if idx % 200 == 0:
            pd.DataFrame(all_pairs).to_csv(OUTPUT_PATH, index=False)
            print(f"  💾 Checkpoint ({len(all_pairs)} pairs)")

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
    print(f"STRATEGY 1 RESULTS")
    print(f"{'=' * 60}")
    print(f"  Input events:              {len(df)}")
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

        # Quality: entity match samples
        entity = df_pairs[df_pairs["alignment_type"] == "entity_match"]
        if len(entity) > 0:
            # Show matches where entities differ (potential problems)
            diff = entity[entity["poly_entity"] != entity["kalshi_entity"]]
            same = entity[entity["poly_entity"] == entity["kalshi_entity"]]
            print(f"\n  Exact entity string matches:   {len(same)}")
            print(f"  Non-exact entity matches:      {len(diff)}")

            if len(diff) > 0:
                print(f"\n  Non-exact matches (ALL — inspect these):")
                print(f"  {'-' * 55}")
                for _, r in diff.iterrows():
                    print(f"  [{r['alignment_score']:.3f}] \"{r['poly_entity']}\" → \"{r['kalshi_entity']}\"")
                    print(f"    Poly:   {str(r['poly_question'])[:65]}")
                    print(f"    Kalshi: {str(r['kalshi_market_title'])[:65]}")
                    print()

            # Random sample of exact matches to verify
            print(f"\n  Exact match samples (15 random):")
            print(f"  {'-' * 55}")
            sample = same.sample(min(15, len(same)), random_state=42)
            for _, r in sample.iterrows():
                print(f"  [{r['alignment_score']:.3f}] \"{r['poly_entity']}\"")
                print(f"    Poly:   {str(r['poly_question'])[:65]}")
                print(f"    Kalshi: {str(r['kalshi_market_title'])[:65]}")
                print()

        print(f"\n  Category breakdown:")
        for cat, count in df_pairs["category"].value_counts().items():
            print(f"    {cat:<25} {count:>5}")

    print(f"\n  ✓ Pairs saved to {OUTPUT_PATH}")
    if len(df_errors) > 0:
        print(f"  ⚠ Errors saved to {ERRORS_PATH}")
    print()


if __name__ == "__main__":
    main()