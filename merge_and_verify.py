# ---- merge_and_verify.py — Merge Strategies + LLM Verification ----
# 1. Concatenates all 5 strategy outputs

import pandas as pd
import json
import time
import os
from datetime import datetime, timezone, timedelta
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY

STRATEGY_FILES = [
    "data/strategy1_pairs.csv",
    "data/strategy2_pairs.csv",
    "data/strategy3_pairs.csv",
    "data/strategy4_pairs.csv",
    "data/strategy5_pairs.csv",
]

OUTPUT_PATH = "data/subscription_manifest.csv"
VERIFICATION_LOG_PATH = "data/llm_verification_log.csv"
SCORE_THRESHOLD = 0.75  # Pairs below this go to LLM verification

# Resolution-date sanity cap: if computed resolution is beyond this many days
SANITY_CAP_DAYS = 365


# ---- DATE PARSING HELPERS ----

def parse_date(s):
    """Parse ISO date/datetime string to UTC datetime. Returns None on failure."""
    if not s or (isinstance(s, float) and pd.isna(s)):
        return None
    s = str(s).strip()
    if not s or s.lower() == "nan":
        return None
    # Try multiple formats — Poly uses ISO with Z, Kalshi uses ISO with offset
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    # Fallback: try fromisoformat (handles more variants in Python 3.7+)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def compute_resolution_date(poly_end_str, kalshi_exp_str):
    """
    Compute effective resolution date for a pair.

    Returns (resolution_date_iso, days_to_resolution, is_capped).
    - Uses max(poly, kalshi) — the LATER of the two, reflecting when capital
      is actually freed (conservative assumption).
    - If either is missing, falls back to the available one.
    - If both missing, returns (None, None, False).
    - If the computed date is more than SANITY_CAP_DAYS away, caps it and
      flags the pair.
    """
    p_dt = parse_date(poly_end_str)
    k_dt = parse_date(kalshi_exp_str)

    if p_dt is None and k_dt is None:
        return None, None, False

    if p_dt is None:
        resolution = k_dt
    elif k_dt is None:
        resolution = p_dt
    else:
        resolution = max(p_dt, k_dt)

    now = datetime.now(timezone.utc)
    days = (resolution - now).total_seconds() / 86400

    is_capped = False
    if days > SANITY_CAP_DAYS:
        is_capped = True
        days = SANITY_CAP_DAYS

    return resolution.isoformat(), round(days, 2), is_capped


def enrich_with_resolution_dates(df):
    """Add resolution_date, days_to_resolution, date_capped columns."""
    res_dates, res_days, caps = [], [], []
    for _, row in df.iterrows():
        poly_end = row.get("poly_end_date", "")
        kalshi_exp = row.get("kalshi_expiration_time", "")
        r_date, r_days, capped = compute_resolution_date(poly_end, kalshi_exp)
        res_dates.append(r_date)
        res_days.append(r_days)
        caps.append(capped)
    df["resolution_date"] = res_dates
    df["days_to_resolution"] = res_days
    df["date_capped"] = caps
    return df


# ---- MERGE ----

def merge_strategies():
    """Concatenate all strategy outputs into one DataFrame."""
    dfs = []
    for path in STRATEGY_FILES:
        if os.path.exists(path):
            df = pd.read_csv(path)
            print(f"  {path}: {len(df)} pairs")
            dfs.append(df)
        else:
            print(f"  {path}: NOT FOUND — skipping")

    if not dfs:
        raise ValueError("No strategy files found!")

    merged = pd.concat(dfs, ignore_index=True)
    print(f"\n  Total after concat: {len(merged)}")
    return merged


def deduplicate_merged(df):
    """
    Remove cross-strategy duplicates.
    Same Kalshi ticker + same poly entity = duplicate.
    Keep the one with the highest alignment score.
    """
    before = len(df)
    df["dedup_key"] = df["kalshi_ticker"].astype(str) + "|" + df["poly_entity"].astype(str)
    df = df.sort_values("alignment_score", ascending=False)
    df = df.drop_duplicates(subset="dedup_key", keep="first")
    df = df.drop(columns=["dedup_key"])
    after = len(df)
    if before != after:
        print(f"  Cross-strategy dedup: {before} → {after}")
    return df.reset_index(drop=True)


# ---- LLM VERIFICATION ----

VERIFICATION_PROMPT = """You are a trading risk analyst verifying whether two prediction market contracts resolve based on the EXACT SAME real-world outcome. This verification gates a live arbitrage system. The cost of errors is asymmetric:
- False MATCH → system trades two contracts that resolve differently → GUARANTEED LOSS
- False REJECT → system skips a potential opportunity → no loss, only missed profit

Because of this, your DEFAULT is REJECT. Only output MATCH when you are 100% certain.

Polymarket question: "{poly_question}"
Kalshi market title: "{kalshi_market_title}"

Polymarket entity (extracted): "{poly_entity}"
Kalshi entity (extracted): "{kalshi_entity}"

Context:
- Polymarket event: "{poly_event_title}"
- Kalshi event: "{kalshi_event_title}"

INSTRUCTIONS — follow these steps in order:

Step 1: IDENTIFY each entity independently. State exactly what real-world person, team, organization, or thing each entity refers to. Do NOT assume they are the same. If you cannot confidently identify what an entity refers to, state "UNKNOWN".

Step 2: COMPARE. Are the two identified entities the exact same real-world thing? Consider:
- Name variants of the SAME person/thing are acceptable (Fred/Frederick, Matt/Matthew, é/e accents, Alex/Alexander, Nottm/Nottingham, Praha/Prague)
- Abbreviations are acceptable ONLY if unambiguous in context (e.g. "Chicago WS" can only mean White Sox in baseball)
- If an abbreviation could refer to MORE THAN ONE entity (e.g. a single letter that multiple teams share), it is AMBIGUOUS → REJECT
- Different people/teams/things that happen to share a word are NOT matches, even if they share a city, first name, or other token
- Different numeric thresholds, timeframes, or resolution conditions mean DIFFERENT contracts → REJECT

Step 3: VERDICT. Output MATCH only if Step 1 produced two confident identifications AND Step 2 confirmed they are the exact same entity. If ANY doubt remains, output REJECT.

Respond with ONLY a JSON object:
{{"poly_identified": "what the Poly entity refers to", "kalshi_identified": "what the Kalshi entity refers to", "verdict": "MATCH" or "REJECT", "reason": "one sentence"}}"""


def verify_pair(client, row):
    """Send a single pair to Claude for verification."""
    prompt = VERIFICATION_PROMPT.format(
        poly_question=str(row["poly_question"])[:200],
        kalshi_market_title=str(row["kalshi_market_title"])[:200],
        poly_entity=str(row["poly_entity"]),
        kalshi_entity=str(row["kalshi_entity"]),
        poly_event_title=str(row["poly_event_title"])[:200],
        kalshi_event_title=str(row["kalshi_event_title"])[:200],
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        # Parse JSON response
        # Handle markdown code blocks
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        verdict = result.get("verdict", "REJECT")
        reason = result.get("reason", "")
        poly_id = result.get("poly_identified", "")
        kalshi_id = result.get("kalshi_identified", "")

        # Safety net: if the LLM identified the entities as different things
        # but still said MATCH, override to REJECT
        if verdict == "MATCH" and poly_id and kalshi_id:
            if "UNKNOWN" in poly_id.upper() or "UNKNOWN" in kalshi_id.upper():
                verdict = "REJECT"
                reason = f"Override: identification uncertain. Poly={poly_id}, Kalshi={kalshi_id}"

        return verdict, reason, poly_id, kalshi_id
    except Exception as e:
        return "REJECT", str(e)[:200], "", ""  # Default to REJECT on error


def run_verification(df):
    """
    Run LLM verification on all sub-threshold pairs.
    Returns (verified_df, log_df).
    """
    needs_verification = df[df["alignment_score"] < SCORE_THRESHOLD].copy()
    auto_accepted = df[df["alignment_score"] >= SCORE_THRESHOLD].copy()

    print(f"\n  Auto-accepted (score ≥ {SCORE_THRESHOLD}): {len(auto_accepted)}")
    print(f"  Needs LLM verification:                  {len(needs_verification)}")

    if len(needs_verification) == 0:
        return df, pd.DataFrame()

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    log_rows = []
    verified_indices = []

    print(f"\n  Running LLM verification...\n")

    for i, (idx, row) in enumerate(needs_verification.iterrows()):
        verdict, reason, poly_id, kalshi_id = verify_pair(client, row)
        log_rows.append({
            "poly_question": str(row["poly_question"])[:100],
            "kalshi_market_title": str(row["kalshi_market_title"])[:100],
            "poly_entity": row["poly_entity"],
            "kalshi_entity": row["kalshi_entity"],
            "poly_identified": poly_id,
            "kalshi_identified": kalshi_id,
            "alignment_score": row["alignment_score"],
            "verdict": verdict,
            "reason": reason,
        })

        if verdict == "MATCH":
            verified_indices.append(idx)

        # Progress
        symbol = "✓" if verdict == "MATCH" else ("✗" if verdict == "REJECT" else "⚠")
        if (i + 1) % 10 == 0 or (i + 1) == len(needs_verification):
            matches = sum(1 for r in log_rows if r["verdict"] == "MATCH")
            rejects = sum(1 for r in log_rows if r["verdict"] == "REJECT")
            errors = sum(1 for r in log_rows if r["verdict"] == "ERROR")
            print(f"  [{i+1}/{len(needs_verification)}] ✓{matches} ✗{rejects} ⚠{errors}")

        time.sleep(0.3)  # Rate limit

    # Combine: auto-accepted + LLM-verified
    llm_verified = needs_verification.loc[verified_indices]
    final = pd.concat([auto_accepted, llm_verified], ignore_index=True)

    log_df = pd.DataFrame(log_rows)
    return final, log_df


# ---- MAIN ----

def main():
    print("=" * 60)
    print("MERGE & VERIFY — Final Subscription Manifest")
    print("=" * 60)

    # Step 1: Merge
    print("\n[Step 1] Merging strategy outputs...\n")
    merged = merge_strategies()

    # Step 2: Deduplicate
    print("\n[Step 2] Deduplicating...\n")
    merged = deduplicate_merged(merged)

    # Score distribution
    print(f"\n  Score distribution:")
    print(f"    1.000:       {(merged['alignment_score'] == 1.0).sum()}")
    print(f"    0.750-0.999: {((merged['alignment_score'] >= 0.75) & (merged['alignment_score'] < 1.0)).sum()}")
    print(f"    0.500-0.749: {((merged['alignment_score'] >= 0.50) & (merged['alignment_score'] < 0.75)).sum()}")

    # Step 3: LLM Verification
    print(f"\n[Step 3] LLM Verification...\n")
    final, log_df = run_verification(merged)

    # Save verification log
    if len(log_df) > 0:
        log_df.to_csv(VERIFICATION_LOG_PATH, index=False)
        matches = (log_df["verdict"] == "MATCH").sum()
        rejects = (log_df["verdict"] == "REJECT").sum()
        errors = (log_df["verdict"] == "ERROR").sum()
        print(f"\n  LLM verification results:")
        print(f"    Matched:  {matches}")
        print(f"    Rejected: {rejects}")
        print(f"    Errors:   {errors}")

    # Step 4: Save final manifest
    # Enrich with resolution date computation
    print(f"\n[Step 4] Computing resolution dates...\n")
    final = enrich_with_resolution_dates(final)

    missing_dates = final["days_to_resolution"].isna().sum()
    capped_dates = final["date_capped"].sum()
    print(f"  Pairs with resolution date:      {len(final) - missing_dates}")
    print(f"  Pairs missing date (both null):  {missing_dates}")
    print(f"  Pairs capped at {SANITY_CAP_DAYS} days:      {capped_dates}")

    if len(final) - missing_dates > 0:
        valid = final[final["days_to_resolution"].notna()]
        print(f"\n  Days-to-resolution distribution (uncapped):")
        uncapped = valid[~valid["date_capped"]]
        if len(uncapped) > 0:
            print(f"    < 7 days:    {(uncapped['days_to_resolution'] < 7).sum()}")
            print(f"    7-30 days:   {((uncapped['days_to_resolution'] >= 7) & (uncapped['days_to_resolution'] < 30)).sum()}")
            print(f"    30-90 days:  {((uncapped['days_to_resolution'] >= 30) & (uncapped['days_to_resolution'] < 90)).sum()}")
            print(f"    90-180 days: {((uncapped['days_to_resolution'] >= 90) & (uncapped['days_to_resolution'] < 180)).sum()}")
            print(f"    180-365 days:{((uncapped['days_to_resolution'] >= 180) & (uncapped['days_to_resolution'] < 365)).sum()}")
            print(f"    Median:      {uncapped['days_to_resolution'].median():.1f}")

    # Select output columns needed by engine
    output_cols = [
        "poly_yes_token", "poly_no_token", "poly_question",
        "kalshi_ticker", "kalshi_market_title",
        "alignment_score", "poly_entity", "kalshi_entity",
        "poly_event_id", "poly_event_title",
        "kalshi_event_ticker", "kalshi_event_title",
        "category",
        "poly_end_date", "kalshi_expiration_time",
        "resolution_date", "days_to_resolution", "date_capped",
    ]
    final = final[output_cols].copy()
    final = final.sort_values(["category", "alignment_score"], ascending=[True, False])
    final = final.reset_index(drop=True)
    final.to_csv(OUTPUT_PATH, index=False)

    print(f"\n{'=' * 60}")
    print(f"FINAL MANIFEST")
    print(f"{'=' * 60}")
    print(f"  Total tradeable pairs: {len(final)}")
    print(f"\n  Category breakdown:")
    for cat, count in final["category"].value_counts().items():
        print(f"    {cat:<30} {count:>5}")
    print(f"\n  Score breakdown:")
    print(f"    1.000:       {(final['alignment_score'] == 1.0).sum()}")
    print(f"    0.750-0.999: {((final['alignment_score'] >= 0.75) & (final['alignment_score'] < 1.0)).sum()}")
    print(f"    0.500-0.749: {((final['alignment_score'] >= 0.50) & (final['alignment_score'] < 0.75)).sum()}")
    print(f"\n  ✓ Manifest saved to {OUTPUT_PATH}")
    if len(log_df) > 0:
        print(f"  ✓ Verification log saved to {VERIFICATION_LOG_PATH}")

    # Cost estimate
    if len(log_df) > 0:
        est_cost = len(log_df) * 0.003  # ~$0.003 per verification call
        print(f"\n  Estimated API cost: ~${est_cost:.2f}")

    print()


if __name__ == "__main__":
    main()