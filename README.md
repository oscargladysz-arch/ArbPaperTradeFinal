# ArbPaperTrade

Cross-platform prediction market arbitrage system. Detects pricing dislocations
between [Polymarket](https://polymarket.com) and [Kalshi](https://kalshi.com),
verifies that paired contracts resolve on the *exact same* real-world outcome,
and paper-trades the spread through live order books.

Buying complementary sides of the same event on two venues for a combined cost
under $1.00 locks in the difference — if and only if both contracts truly settle
on the same outcome. Most of this codebase exists to make sure they do.

## Pipeline

```
confirmed_matches.csv          (event-level matches from a separate mapping stage:
        |                       TF-IDF candidate generation + LLM verification)
        v
strategy1..5_resolver.py       align sub-markets within each matched event
        |                      (entity extraction, boundary matching, guards)
        v
merge_and_verify.py            dedup across strategies + conservative LLM pass
        |                      on low-confidence pairs -> subscription_manifest.csv
        v
engine.py                      live WebSocket books on both venues, VWAP walk,
                               fee model, time-value filter -> paper ledger
```

The five resolvers are category-specific because alignment logic differs by
market structure:

| Resolver | Categories | Core method |
|---|---|---|
| 1 | Elections, Politics, Entertainment | shared-scaffolding entity extraction |
| 2 | Sports head-to-head | winner-market filtering, draw exclusion |
| 3 | Sports awards/standings/drafts | entity extraction + sport-tag verification |
| 4 | Economics, Climate | exact numeric boundary matching |
| 5 | Companies, Financials, Crypto, World | hybrid entity/boundary |

## Design principle: precision over recall

Arbitrage margins are thin. One mismatched pair — two contracts that *look*
identical but resolve differently — produces a guaranteed loss that can erase
dozens of correct trades. Every guard in this system trades recall for
precision:

| Guard | Catches |
|---|---|
| Draw filter (S2) | draw contracts matched against winner markets |
| Numeric bracket guards | "5-10%" matched to "0-5%", "Season 10" to "Season 2" |
| Directional polarity | "<70 seats" matched to "above 70 seats" |
| Semantic polarity | "relocated away from Mexico" vs "played in Mexico" |
| Sport-tag verification | NHL Eastern Conference vs NBA Eastern Conference |
| Ticker-year guard | 2026 special election matched to a 2028 cycle market |
| Bundled-entity rejection | one candidate matched against a multi-state basket |
| LLM verification pass | low-confidence pairs; defaults to REJECT |
| Dynamic fee model | Kalshi's P*(1-P) taker fee, computed per trade |
| Time-value filter | spreads that lose to T-bills over the holding period |
| Long-dated cap | markets whose true horizon is years out (2030+ closes) |
| Suspect-spread cap | "arbs" >15% gross — stale books, not free money |
| Event over-sum detector | multi-candidate events where Kalshi YES prices sum past $1 |
| Staleness + depth checks | books older than 5s or thinner than 2 levels |

Iterating on these guards took the system from 102 phantom trades in an early
30-minute run to zero across four structural bug classes.

## Fee model

Kalshi charges takers `ceil_to_cent(0.07 * contracts * P * (1-P))` — about
1.75% of notional at $0.50, near zero at the tails. As a fraction of notional
the burden is `~0.07 * (1-P)`, which makes the *cheap* leg of a pair the
expensive one to trade. A flat fee buffer gets this exactly backwards; the
engine computes fees from actual VWAP prices per leg.

## Time-value filter

Locked capital has opportunity cost. A trade only fires if

```
spread_profit >= RISK_FREE_RATE * (days_to_resolution / 365)
```

Resolution dates come from Polymarket `endDate` and Kalshi `expiration_time`
(the later of the two, capped and flagged at 365 days). Capped pairs are
evaluated against an assumed multi-year lockup, which prices most of them out —
intentionally.

## Sample results (paper)

A 24-minute session on ~2,900 subscribed pairs: 21 trades across 16 positions,
mean gross spread 6.1%, zero trades above the suspect threshold. Paper fills at
VWAP are optimistic — live fill rate, exit liquidity, and adverse selection are
unvalidated and are the next thing this project measures.

## Setup

```bash
pip install -r requirements.txt
cp config.example.py config.py     # add your keys — config.py is gitignored
python strategy1_resolver.py       # ... through strategy5
python merge_and_verify.py
python engine.py
```

Requires a Kalshi API key (RSA key pair) and an Anthropic API key for the
verification pass. The mapping stage that produces `confirmed_matches.csv`
runs separately in a notebook.

## Disclaimer

Educational project. Paper trading only. Nothing here is financial advice, and
past paper spreads say nothing about live profitability.
