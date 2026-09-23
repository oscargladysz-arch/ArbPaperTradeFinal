# PREREGISTRATION.md, version 1 (DRAFT until Oscar signs)

Signing: Oscar signs by approving the commit that contains this file. That commit hash is
recorded in every report and in every `research/runs.jsonl` entry (`prereg_hash` is the SHA-256
of this file). Any later change is a new numbered version with a stated reason, and results are
reported under every version.

Windows: in-sample 2024-01-01 to 2026-06-22 inclusive. Holdout 2026-06-23 to 2026-09-22
inclusive, locked by `research/holdout.lock` until Oscar deletes it.

## 1. Hypotheses

- H0: net markout <= 0.
- H1 (primary): Kalshi fills priced at least 3 cents better than Polymarket fair value (FV)
  earn positive net FV markouts after maker fees and adverse selection.
- H2 (secondary): the effect is stronger on the favorite side (maker's contract price above
  0.50), consistent with the favorite-longshot bias. Test statistic: the difference in
  contract-weighted mean net 1-hour FV markout in the edge >= +3 cents bucket between fills
  with maker paid price >= 0.50 and fills below 0.50, with a block-bootstrap 95% CI; H2 is
  supported only if the CI lower bound > 0. Every other horizon and bucket cell is descriptive.

## 2. Definitions (all prices in YES terms, stored as integer ticks, 1 cent = 100 ticks)

### 2.1 Maker side
Kalshi trades report `taker_side`.
- `taker_side = no`: the taker bought NO, so the maker bought YES at `yes_price`.
- `taker_side = yes`: the taker bought YES, so the maker bought NO at `1 - yes_price`.

### 2.2 Fair value (FV), Tiers 0 and 1
FV at time t is the midpoint of the latest taker-buy print and the latest taker-sell print on
Polymarket within the 10 minutes before t - L_ref, with L_ref = 2 seconds. If only one side
printed, FV is that print. If nothing printed, there is no FV and the fill is excluded.

Every Polymarket print is converted to YES-token terms before use: a taker BUY of the NO token
at price q is a taker SELL of YES at 1 - q, and a taker SELL of the NO token at q is a taker BUY
of YES at 1 - q. (Decision D1 below.)

Worked example: within the window there is a taker buy of YES at 0.50 and a taker sell of the
NO token at 0.52. The NO sell converts to a YES buy at 1 - 0.52 = 0.48, which is more recent
than the 0.50 print, so the latest taker-buy is 0.48. The latest taker-sell of YES is 0.47.
FV = (0.48 + 0.47) / 2 = 0.475.

### 2.3 Edge
- YES maker: edge = FV - yes_price.
- NO maker: edge = yes_price - FV.
Example A: yes_price 0.44, taker_side no, FV 0.49: edge = 0.49 - 0.44 = +0.05.
Example B: yes_price 0.62, taker_side yes, FV 0.58: edge = 0.62 - 0.58 = +0.04.

### 2.4 Fee
Charged fee per order = round_up_to_cent(M_maker * 0.0175 * C * P * (1 - P)) dollars, with P
the price the maker paid (YES price for a YES maker, 1 - yes_price for a NO maker), C the
contracts in the fill, and M_maker the series maker multiplier in force on the fill date
(0 on series without maker fees). Per-contract fee = charged fee / C. (Decision D2.)

Unit of rounding: Kalshi rounds per order, but maker order ids are not public, so the research
rounds per print (each public trade is treated as one order). Per-print rounding is an upper
bound on the per-order charge (a resting order filled in several prints pays one rounding, not
several). The unrounded rate M_maker * 0.0175 * P * (1 - P) is reported alongside as the
lower-bound sensitivity. Gate L compares the live fee model with the ledger per order, not with
this per-print number. Fractional contract counts (count_fp) are rounded the same way.

Example A, 50 contracts at 0.44: raw = 1 * 0.0175 * 50 * 0.44 * 0.56 = 0.2156 dollars,
charged = 0.22 dollars, per contract = 0.22 / 50 = 0.0044.
Example A, 1 contract: raw = 0.004312, charged = 0.01, per contract = 0.01.
Example B, 1 contract at 0.38: raw = 0.0175 * 0.38 * 0.62 = 0.004123, charged 0.01.

### 2.5 Markouts and settlement return
At horizon h in {1m, 5m, 1h, 24h}:
- YES maker: markout_h = FV(t + h) - yes_price - fee.
- NO maker: markout_h = yes_price - FV(t + h) - fee.
FV(t + h) uses the same 10-minute window rule ending at t + h - L_ref. If no FV exists at
t + h the markout at that horizon is missing and excluded from that horizon's statistics; the
exclusion rate per horizon is reported. (Decision D3.)

Settlement return, outcome in {0, 1}:
- YES maker: outcome - yes_price - fee.
- NO maker: (1 - outcome) - (1 - yes_price) - fee.
Example A (50 contracts, fee 0.0044 per contract): YES settles 1: 1 - 0.44 - 0.0044 = +0.5556.
Settles 0: 0 - 0.44 - 0.0044 = -0.4444.

Kalshi-mid markouts (secondary) are computed only when the Kalshi spread at t is <= 10 cents,
from the 1-minute candle bid and ask closes.

## 3. Primary and secondary metrics

- Primary: contract-weighted mean net 1-hour FV markout per contract in the edge >= +3 cents
  bucket, pooled across price buckets and categories. Contract-weighted means each fill
  contributes its contract count as weight. (Decision D4.) Fill-weighted mean is reported as a
  sensitivity, never as the gate.
- Secondary: the same at 1m, 5m, 24h, and settlement; by price bucket (H2 compares the
  0.50 to 0.97 favorite buckets against the 0.03 to 0.50 buckets); the stale-maker cost in the
  edge <= -3 cents bucket; lead-lag regression; coarse pick-off race.

## 4. Buckets
- Edge: (<= -3), (-3, -1], (-1, +1), [+1, +3), (>= +3) cents.
- Contract price (maker's paid price): [0.03, 0.20), [0.20, 0.50), [0.50, 0.80), [0.80, 0.97].
- Category: Kalshi's series `category` field (Decision D5).

## 5. Fixed exclusions (never tuned)
1. Sports fills at or after the scheduled start time, taken from Kalshi's `occurrence_datetime`
   or Polymarket's `gameStartTime` captured ex ante; where neither exists the fill stays in.
2. Fills in the final 30 minutes before the SCHEDULED end: Kalshi `expected_expiration_time`
   (fallback the legacy `expiration_time`), never `settlement_ts` and never `close_time`, which
   Kalshi moves earlier once an outcome is determined. The same scheduled field defines the
   pair date-window check and the market-window filter of the loaders.
3. Maker paid prices outside [0.03, 0.97].
4. Any fill without a valid FV at t - L_ref.
5. Pairs excluded only for a documented rules-based reason recorded in `data/approved_pairs.csv`.
   Price paths and divergent resolutions are never reasons.

## 5a. Pair universe, frozen (audit findings C15, C16)
- Candidate blocking: token Jaccard >= 0.25 between the Polymarket question and the Kalshi title
  plus yes_sub_title, and scheduled ends within 45 days.
- Deterministic checks: underlying Jaccard >= 0.60 PASS, 0.34 to 0.60 UNKNOWN, below FAIL;
  thresholds equal with unit and strictness; scheduled ends within 3 days PASS, 3 to 45 days
  UNKNOWN, beyond FAIL; resolution source and tie handling as in `resolution.py`.
- Side alignment must be RESOLVED deterministically (`sides.py`); inverted pairs are rejected,
  never flipped.
- LLM pass on every surviving pair: model id recorded in `research/runs.jsonl` by the mapping
  run; default REJECT; a MATCH requires every dimension true and side alignment "same".
- One-to-one: a Kalshi market with more than one accepted counterparty, or vice versa, is
  rejected, never resolved by score.
- The universe is every row of `data/candidate_pairs.csv` with decision ACCEPT from a full run
  (no `--limit`, no `--offline`; the run's SHA-256 of that file is in the ledger) minus the pairs
  Oscar rejects in `data/approved_pairs.csv`, and only after a review round with zero errors in
  the random sample. If a fix is needed, the root cause is fixed, the pipeline re-run in full,
  and a new sample drawn with seed 20240101 + round number; all pairs approved in earlier
  rounds stay approved. Tier 0 refuses to run unless the approved_pairs hash it reads matches
  a ledger entry from such a run.
- The review page never shows settlement results or price paths (except the KAT-3 overlay
  for flagged pairs, whose flag is itself pre-registered).

## 6. Gates (verbatim from the spec)
- Gate 0: in the >= +3 cent bucket, mean net 1-hour FV markout > 0 with CI lower bound > 0, on
  >= 1,000 fills, >= 30 events, >= 3 categories, with no event above 25% of bucket P&L. Sanity
  condition: the <= -3 cent bucket mean is < 0.
- Gate 1: the selected variant has toxic net P&L per contract > 0 with CI lower bound > 0 and
  >= 500 fills, the ordering optimistic >= pessimistic >= toxic holds, and annualized return
  >= T-bill + 10 points.
- Gate 2: the Phase 2 variant, unchanged, has net P&L per contract > 0 with CI lower bound > 0
  under the conservative queue model at placeholder latency, and >= 0 at twice that latency. If
  the pick-off race p50 is faster than our latency, the strategy is reported infeasible.
- Gate H: Tier 1 toxic-rule net P&L per contract of the frozen variant on the holdout, on
  >= 200 holdout fills, point estimate > 0 and >= 50% of the in-sample toxic estimate; the Tier 0
  edge >= +3 cents 1-hour markout on the holdout is reported alongside and its sign must agree.
- Gate L: >= 200 live fills, realized net FV markout > 0 (CI reported), realized fill rate and
  markouts inside the simulator's 90% prediction intervals, fee model error of 0 cents per
  order, zero risk-limit breaches.

## 7. Frozen grid and selection rule (Phase 2)
48 variants: e in {2, 3, 5} cents * placement in {cap, penny} * sides in {favorite-only, both}
* delta in {1, 2} cents * inventory cap in {25, 100} contracts per market.
Selection: the variant with the highest 95% CI lower bound on net P&L per contract under the
toxic fill rule, among variants with >= 500 fills. Final once chosen.

## 8. Statistics
- 95% confidence intervals: block bootstrap, 10,000 resamples, percentile method, fixed seed
  20240101, under two block definitions: (a) Kalshi event (event_ticker) and (b) the coarser
  series_ticker by calendar day. Gates use the coarser block (b); (a) is reported alongside.
  Gate 0 additionally requires >= 10 distinct series_tickers in the bucket. The seed and
  resample count are asserted at gate time and written to the ledger; a lower bound within 0.05
  cents of zero is reported with a 20-seed sensitivity band and treated as inconclusive if any
  seed flips it.
- Markout standard deviation is reported. Power: for the primary metric, the minimum
  detectable effect at 80% power and alpha 0.05 is computed from the observed SD and the
  effective sample size (events). If the observed mean is below the MDE, the result is
  "inconclusive", never "pass".
- Regime splits: before and after 2026-03-30 (Polymarket fee rollout), before and after
  2026-04-28 (CLOB V2), and by calendar year. Both dates are ASSUMPTIONS P-FEE-1 and P-CLOB-1
  and are verified before the Phase 1 report.
- Risk-free rate: FRED DTB3 averaged over the evaluation window, simple average of daily
  observations. Example: window 2026-01-01 to 2026-06-22, daily rates averaging 4.20 -> 4.20%.

## 9. Data
Kalshi public REST via the `/historical/*` namespace (the historical cutoff of 2026-07-25
covers the whole in-sample window): trades with `taker_outcome_side`, 1-minute candles,
archived market records with `result`, `settlement_ts`, and the closing book; fee regimes per
ASSUMPTIONS K-FEE-7. Polymarket Gamma keyset metadata and Data API v2 trades; on-chain fills
as a completeness check; Predexon ticks for Tier 2 (in-sample slices only until the holdout
unlocks). Every loader refuses holdout rows while the lock exists.

## 9a. KAT-1 method (frozen; ASSUMPTIONS KAT-1)
Window 2021-06-01 to 2025-04-30 (paper: inception through April 2025). Universe: settled
markets with volume_fp >= 1,000 contracts, closing spread <= 20 cents, open >= 24 hours.
Observations: last trade before close and the last trade before the same time on each of up
to 10 prior days; two contracts per trade (taker's and maker's). Returns: pre-fee
(y - p) / p; post-fee (y - p - c) / (p + c), c = round_up_to_cent(0.07 * 100 * p * (1 - p)) / 100
applied to both sides (the paper's imputation). Equal-weighted means. Pass: makers within 3
points of -11.99% and takers within 3 points of -31.46%, makers > takers. Worked example: last
trade yes_price 0.44, taker_side no, settles YES. Maker holds YES at 0.44: c = 1.73 / 100 =
0.0173; post-fee return = (1 - 0.44 - 0.0173) / (0.44 + 0.0173) = +1.1868. Taker holds NO at
0.56: c = 0.0173; post-fee return = (0 - 0.56 - 0.0173) / (0.56 + 0.0173) = -1.0.

## 10. Decisions that require Oscar's sign-off (defaults in force until changed before signing)

| id | Decision | Default in force |
|---|---|---|
| D1 | NO-token prints convert to YES terms before the buy/sell midpoint. | Yes. |
| D2 | Fee is the exact charged fee (per-order round-up to the cent) allocated per contract, not the unrounded rate. | Exact charged fee. Spec worked examples are restated in 2.4. |
| D3 | Missing FV(t + h) excludes that horizon's markout; exclusion rate reported. | Exclude. |
| D4 | Primary mean is contract-weighted. | Contract-weighted. |
| D5 | Category taxonomy is Kalshi's series category. | Kalshi category. |
| D6 | Money resolution is 1/100 cent ticks; Kalshi sub-penny markets are handled at their tick size. | Yes. |
| D7 | Kalshi series whose `frequency` is hourly or fifteen_min are outside the universe (their markets live under 24 hours; they are 83% of the archive by count; the KAT-1 paper excludes them; the 10-minute FV window and the 30-minute pre-settlement exclusion leave almost nothing tradeable in them). This is a structural, ex-ante exclusion, never a price-based one. | Excluded. |
| D8 | Kalshi block trades (`is_block_trade = true`) are excluded from the strategy analysis (they are negotiated off-book, so no resting quote could have been filled). KAT-1 keeps them, as the paper does not exclude them. | Excluded from Tier 0 to 2; kept in KAT-1. |
| D9 | Maker fee regime by date follows ASSUMPTIONS K-FEE-7: no maker fees before 2025-05-13, then the archived schedule lists, then the API fee-change feed. Fills before a market's `fee_waiver_expiration_time` are fee-free. | Yes. |
| D10 | Polymarket prints come from Data API v2 (`/v2/trades`, taker side, cursor-paged, no cap); on-chain fills are a completeness check on a sample, not the primary source. | Yes. |

## 11. Change log
- v1, 2026-09-23: initial draft.
- v1 (same day, still unsigned): added D7 to D10 and section 9a after verifying the APIs, the fee schedules, and the paper.
- v1 (same day, still unsigned): after the rule-12 bias audit (run wf_247ab9a4-551): fee rounding unit stated (2.4), exclusion fields frozen (5), pair universe frozen (5a), H2 statistic, Gate H specification, two-level block bootstrap and seed policy (8).
