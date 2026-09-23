# SPEC ref_mm (verbatim, as received from Oscar on 2026-09-23)

## 0. Standing rules
If CLAUDE.md exists, keep it and add anything below that is missing. Otherwise create it with these rules.
1. Precision over recall. A mapping error, side inversion, or look-ahead leak is an existential bug. A missed opportunity is acceptable.
2. Generalizable logic only. No event-specific patches. Root-cause fixes only.
3. Deterministic logic handles clear structure. LLM output is schema-validated, cached, auditable, and never the only guard.
4. Verify every API, fee, data-source, and settlement assumption against current official docs before coding it. Log each verified fact with source URL and date in docs/ASSUMPTIONS.md. Observed behavior beats docs, and the discrepancy gets logged.
5. Money math in integer ticks or Decimal. Never float.
6. Docs and reports use * for multiplication and / for division, with a worked numerical example for every formula.
7. Never disable TLS verification.
8. Paper mode is the default. Live mode requires a manual arming step by Oscar (CLI flag plus a hand-edited config value). Never arm it yourself.
9. Private repository. Secrets in AWS SSM Parameter Store in production and a git-ignored .env locally, with a gitleaks pre-commit hook.
10. Legal scope: Oscar is a US person. Polymarket Global is read-only (public APIs and public blockchain data). No Global accounts, no Global orders, no VPNs, no non-US hosting. Orders go only to Kalshi.
11. Research integrity: follow Section 4 exactly. Never read holdout data, never remove research/holdout.lock, and never change a frozen item without a new pre-registration version that Oscar signs.
12. Ultracode discipline: use workflows for design, bias audits, independent re-derivation, the replay adapter, and report verification. Never use agent loops for bulk downloads. Write resumable, checkpointed scripts, run them in the background, and check progress cheaply. Each phase report lists the workflow runs used and what they did.
13. Gated delivery: at the end of each phase write docs/reports/PHASE_<n>.md (numbers, plots, every variant tested, what failed, go/no-go against the gate) and stop until Oscar approves.

## 1. What we are testing
Strategy: on Kalshi markets that map one-to-one to liquid Polymarket Global markets, rest Kalshi bids priced at least e cents better than Polymarket's fair value (FV), move or pull them when FV moves, and hold inventory to settlement unless opposite fills net it down.
H1 (primary): Kalshi fills at prices at least 3 cents better than FV earn positive net markouts after maker fees and adverse selection.
H2 (secondary): the effect is stronger on the favorite side (contract price above 0.50), consistent with the favorite-longshot bias.
H0: net markout <= 0.
Definitions, all in YES-price terms:
- Maker side: Kalshi trades report taker_side. taker_side = no means the taker bought NO, so the maker bought YES at yes_price. taker_side = yes means the maker bought NO at 1 - yes_price.
- Edge: YES maker edge = FV - yes_price. NO maker edge = yes_price - FV.
- Maker fee per contract = M_maker * 0.0175 * P * (1 - P), where P is the price paid and M_maker is the series multiplier in force that day (0 on series without maker fees).
- Markout at horizon h: YES maker = FV(t + h) - yes_price - fee. NO maker = yes_price - FV(t + h) - fee.
- Settlement return: YES maker = outcome - yes_price - fee. NO maker = (1 - outcome) - (1 - yes_price) - fee, with outcome in {0, 1}.
Worked example A: 50 contracts print at yes_price 0.44 with taker_side = no, so the maker bought YES at 0.44. FV at t - 2s = 0.49, so edge = 0.49 - 0.44 = +0.05. Fee = 0.0175 * 0.44 * 0.56 = 0.0043. FV at t + 1h = 0.47, so markout = 0.47 - 0.44 - 0.0043 = +0.0257 per contract. Settlement: +0.5557 if YES, -0.4443 if NO.
Worked example B: a print at yes_price 0.62 with taker_side = yes, so the maker bought NO at 0.38. FV = 0.58, so edge = 0.62 - 0.58 = +0.04. Fee = 0.0175 * 0.38 * 0.62 = 0.0041. FV at t + 1h = 0.60, so markout = 0.62 - 0.60 - 0.0041 = +0.0159.
Markouts are measured against FV, not Kalshi's mid, because Kalshi's mid is noise when its spread is wide, which is exactly where this strategy quotes. Kalshi-mid markouts are secondary, computed only when the Kalshi spread <= 10 cents.

## 2. Universe and mapping
- Extend the ArbPaperTrade mapper (repo plus Phase_1_Mapper_v3.ipynb) to resolved markets in the research window: Kalshi /historical markets, and Polymarket closed markets via Gamma keyset pagination.
- Tradeable pairs: IDENTICAL resolution only (same underlying, threshold, date window, resolution source, tie and cancellation handling). Side alignment comes from Polymarket outcome labels versus Kalshi yes_sub_title and rules, never from token index.
- Ex-ante rule: a pair can be excluded only for a documented rules-based reason. Never exclude a pair because of its price path or because the venues resolved differently. Divergent resolution is a real cost and stays in the sample.
- Review page: build a local HTML page showing, per pair, both titles, rules excerpts, outcome labels, and the side alignment. For KAT-3-flagged pairs only, add an overlay of Kalshi mid and FV. The page restates that a rejection must cite a rules-based reason. Oscar's decisions save to data/approved_pairs.csv.
- Acceptance: Oscar reviews a stratified random sample of at least 100 auto-verified pairs plus every KAT-3-flagged pair. Zero errors in 100 bounds the error rate near 3% at 95% confidence, and the auto-verified universe is accepted. Any error means fix the root cause, re-verify, and re-sample.
- Fixed exclusions (never tuned): sports fills after scheduled start, fills in the final 30 minutes before scheduled settlement, contract prices outside [0.03, 0.97], and any fill without a valid FV.

## 3. Data
- Kalshi (public endpoints, no key): events, markets, /historical/* for archived data, trades with taker_side, 1-minute candles (yes_bid and yes_ask OHLC carry price without size), settlements. Fees: series fee_type and fee_multiplier, GET /series/fee_changes with show_historical=true, GET /events/fee_changes, and fee waivers. Price each fill at the fee regime in force that day.
- Polymarket Global (read-only): Gamma metadata, Data API trades (reported offset cap of 10,000, and some time filters reportedly ignored, so verify which parameters work), and on-chain fills via ClickHouse CryptoHouse, Dune, or Goldsky as a completeness check (parse CLOB V1 and V2 schemas). Do not use /prices-history for historical FV. Resolved markets reportedly return only 12-hour bars, and its 1-minute series behaves like a mirrored midpoint.
- FV for Tiers 0 and 1: the midpoint of the latest taker-buy and taker-sell Polymarket prints within the 10 minutes before t - L_ref, with L_ref = 2 seconds. If only one side printed, use that print. If nothing printed, there is no FV and the fill is excluded.
- FV for Tier 2 and live: the Polymarket book mid, valid only when the spread <= 2 cents and the last book update is <= 5 seconds old.
- Tick data (Tier 2): Predexon has captured Polymarket and Kalshi order books tick by tick since January 2026, with millisecond timestamps. Pricing: $40/GiB Polymarket, $80/GiB Kalshi, $5 minimum, free quotes, $50 starter credits. Oscar creates the account and supplies the key. Quote every slice first and propose a purchase plan with dollar totals. Default ceiling $150 unless Oscar raises it. Buy in-sample slices only until the holdout unlocks.
- Vendor validation: vendor fills must match Kalshi's official trade tape on at least 99% of trades by count and by volume within 1 second. Otherwise stop and report.
- Phase 0 blocker, US reachability: confirm that Polymarket Global's public REST endpoints and market WebSocket respond from Oscar's machine and from AWS us-east-1 with no circumvention. If they refuse US traffic, stop and report, because the live strategy would have no reference price. The fallback to evaluate is Polymarket US data for markets it lists.
- Risk-free rate: FRED series DTB3, averaged over each evaluation window.

## 4. Research integrity protocol
- Windows: in-sample 2024-01-01 to 2026-06-22. Holdout 2026-06-23 to 2026-09-22.
- Holdout lock: create research/holdout.lock in Phase 0. Every data loader refuses rows timestamped on or after 2026-06-23 while the lock exists, and logs each refused attempt. Only Oscar deletes the lock.
- Known-answer tests, run before any strategy analysis:
  KAT-1: locate the 2025 paper "Makers and Takers: The Economics of the Kalshi Prediction Market" (Whelan and coauthors) and its CEPR summary. Replicate its sample window, contract universe, and return definition exactly, then reproduce its headline averages (takers lose almost 32%, makers about 10%). Pass: each within 3 percentage points, with makers > takers.
  KAT-2: reproduce the favorite-longshot pattern (average returns rise with contract price bucket).
  KAT-3: for candidate pairs, report the correlation and median absolute gap between Kalshi mid and FV. Negative correlation flags a pair for side-inversion review.
  If KAT-1 or KAT-2 fails, stop. The maker attribution or return math is wrong, and every later number would be noise.
- Pre-registration: write docs/PREREGISTRATION.md in Phase 0 covering hypotheses, primary and secondary metrics, buckets, the frozen grid, the selection rule, gates, exclusions, and statistical methods. Oscar signs by approving a commit. Record that hash in every report. Any later change is a new version with a stated reason, and results are reported under every version.
- Runs ledger: append every analysis run to research/runs.jsonl (config hash, code commit, window, metrics). Reports include every run, not just the best.
- Independent re-derivation: for each phase's headline number, a separate workflow re-implements the computation from raw data without reading the primary code. The two must agree within 0.1 cent per contract before the report ships.
- Statistics: block bootstrap by event (10,000 resamples) for 95% CIs. Measure markout SD and report power. An underpowered result is "inconclusive", never "pass".
- Regime breaks: split results at Polymarket's fee rollout (2026-03-30) and the CLOB V2 launch (2026-04-28), and by year. Verify both dates.

## 5. Phase 1: Tier 0 real-fill study (in-sample)
- For every Kalshi trade in approved pairs, compute: maker side, price, FV at t - L_ref, edge, fee, FV markouts at +1m, +5m, +1h, +24h, settlement return, and Kalshi-mid markouts where valid.
- Buckets: edge (<= -3, -3 to -1, -1 to +1, +1 to +3, >= +3 cents) by contract price (0.03 to 0.20, 0.20 to 0.50, 0.50 to 0.80, 0.80 to 0.97) by category.
- Secondary: the stale-maker cost (the <= -3 cent bucket). A lead-lag check (Kalshi mid change over the next 5 minutes regressed on FV change over the prior 5 minutes). A coarse pick-off race (time from an FV move of 3 cents or more to the first Kalshi print at the stale price).
- Gate 0: in the >= +3 cent bucket, mean net 1-hour FV markout > 0 with CI lower bound > 0, on >= 1,000 fills, >= 30 events, >= 3 categories, with no event above 25% of bucket P&L. Sanity condition: the <= -3 cent bucket mean is < 0. If it isn't, Polymarket doesn't lead on this universe and the premise fails.

## 6. Phase 2: Tier 1 minute-bar quote simulation (in-sample)
- Book state comes from 1-minute Kalshi candles (best NO bid = 1 - YES ask), fills from both tapes, FV from Section 3.
- Quote placement: "cap" rests the YES bid at FV - e, rounded down to tick. "penny" rests it at min(FV - e, best YES bid + 1 tick). NO-side quotes mirror this using 1 - FV. Example: FV 0.55, e = 0.03, Kalshi 0.45 bid / 0.65 ask. Cap bids 0.52. Penny bids min(0.52, 0.46) = 0.46.
- Requote when FV moves >= delta since the last quote. Favorite-only quotes only the side whose contract fair value is >= 0.50. Both quotes both sides, and opposite fills net inventory.
- Fill rules, all three reported: optimistic (the candle range touched the quote), pessimistic (a print went strictly through the quote), toxic (pessimistic, plus any minute where FV crossed the quote counts as a fill at the quote).
- Frozen grid, 48 variants: e in {2, 3, 5} cents * placement in {cap, penny} * sides in {favorite-only, both} * delta in {1, 2} cents * inventory cap in {25, 100} contracts per market.
- Capital metric: sum of price paid * days held. Annualized return = net P&L / (capital-days / 365).
- Selection rule: the variant with the highest CI lower bound on net P&L per contract under the toxic rule, among variants with >= 500 fills. This choice is final.
- Gate 1: the selected variant has toxic net P&L per contract > 0 with CI lower bound > 0 and >= 500 fills, the ordering optimistic >= pessimistic >= toxic holds, and annualized return >= T-bill + 10 points.

## 7. Phase 3: Tier 2 tick replay (2026-01-01 to 2026-06-22)
- Data: Predexon raw or 100 ms ticks for approved pairs, in-sample only, validated against Kalshi's tape.
- Engine: hftbacktest (github.com/nkaz001/hftbacktest). Convert Kalshi's two-sided book into one ladder (a NO bid at q is a YES ask at 1 - q), use each market's tick size, and feed the Polymarket book as a second asset for FV. Latency: start at p50 250 ms and p99 1 s as placeholders, to be replaced by Phase 5 measurements. Queue: run the most conservative model (queue advances only on trades) and one probabilistic model, and report both. Compute fees post hoc with the exact Kalshi formula.
- Measure: the pick-off race at tick resolution (time from an FV move of 2 cents or more to the first Kalshi print at the stale price, p10/p50/p90), and FV proxy error (trade-based FV versus tick mid, then re-run Gate 0 with tick FV).
- Gate 2: the Phase 2 variant, unchanged, has net P&L per contract > 0 with CI lower bound > 0 under the conservative queue model at placeholder latency, and >= 0 at twice that latency. If the pick-off race p50 is faster than our latency, report the strategy as infeasible.

## 8. Phase 4: holdout, one shot
- Oscar reviews Phases 1 to 3 and deletes research/holdout.lock.
- Run the frozen variant, unchanged, through the Tier 0 metrics and the Tier 1 simulation on the holdout, plus Tier 2 if Oscar approves that data purchase.
- Gate H: holdout net P&L per contract > 0 and >= 50% of the in-sample point estimate. Report power. No retuning afterward. Any change needs a new pre-registration and is tested only on forward shadow data.

## 9. Phase 5: shadow, then micro-live
- Build: a Kalshi feed (authenticated WebSocket for orderbook deltas with sequence checks, trades, lifecycle, and fills), a Polymarket market WebSocket for FV, a quoter, a risk manager, and a ledger. Run them as systemd services on one AWS t4g.small, in the region chosen by measured round-trip time to both venues, with Parquet to S3. Oscar supplies the Kalshi key and AWS account.
- Shadow, 14 days: log every intended quote and score it against the live tape with the Phase 3 simulator. Measure real latency (Polymarket message to Kalshi order acknowledgment) and feed it back into the simulator.
- Micro-live, only after Oscar arms it: 1 to 5 contracts per quote, $250 maximum total exposure, a dedicated Kalshi subaccount, post-only Orders V2 with client_order_id, and Order Groups as the exchange-side cap, for 2 to 4 weeks.
- Gate L: >= 200 live fills, realized net FV markout > 0 (CI reported), realized fill rate and markouts inside the simulator's 90% prediction intervals, fee model error of 0 cents per order, and zero risk-limit breaches. Then $1,000, then scale only with Oscar's approval.

## 10. Live risk controls
Pull every quote on a market when any of these happens: FV is stale for more than 5 s, the Polymarket spread exceeds 2 cents, FV moved >= delta and the replacement isn't acknowledged within the latency budget, the Kalshi feed shows a sequence gap, the pair's rules hash changes, a sports start time arrives, or settlement is 30 minutes out. Caps: per-market inventory, per-event exposure, total gross exposure, a daily loss halt, and a 15% drawdown halt. A kill file triggers Cancel All Orders. Every halt alerts Oscar (SNS email plus a Telegram or Slack webhook).

## 11. Architecture
Reuse pmcore/ if it exists (venues/kalshi, book, recorder, replay, risk, ledger, alerts, config). Otherwise create it. strategies/ref_mm/ holds research/tier0, research/tier1, research/tier2, sim/ (minute simulator, hftbacktest adapter), and live/ (fv, quoter, executor). Data lake: Parquet partitioned by venue, date, and market, queried with DuckDB or Polars. Python 3.12. pytest with hypothesis property tests (a quote never sits above FV - e, fees match the formula, inventory caps hold, the loader refuses holdout rows). mypy strict on pmcore.

## 12. Timeline and deliverables
Phase 0: 2 to 4 days. Phase 1: 1 to 3 days. Phase 2: 1 to 2 days. Phase 3: 3 to 7 days. Phase 4: 1 day. Phase 5: 14 days of shadow plus 2 to 4 weeks of micro-live. Every phase ships code, tests, its report, runs.jsonl entries, and CLAUDE.md updates, then stops for approval.
