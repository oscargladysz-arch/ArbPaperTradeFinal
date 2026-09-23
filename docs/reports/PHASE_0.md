# PHASE 0 report (foundations, data plumbing, known-answer tests, mapping)

Status (updated 2026-09-23 23:30 UTC): **environment unblocked, assumptions verified, data
downloads running in the background, KATs and mapping not yet run.** Go/no-go on the Phase 0
blocker (US reachability of Polymarket Global): **PASS from a US cloud IP** (public REST, CLOB
book reads, and the market WebSocket all answered; see docs/observed/probe_cloud_us_oh.json),
**pending** from Oscar's machine and from us-east-1.

Pre-registration: `docs/PREREGISTRATION.md` v1 is a DRAFT. It is signed when Oscar approves
the commit that contains it; that hash is then recorded here and in every runs.jsonl entry.

## 1. What shipped

| Area | Files | State |
|---|---|---|
| Spec and rules | `docs/SPEC_ref_mm.md` (verbatim), `CLAUDE.md`, `pyproject.toml`, `.pre-commit-config.yaml` (gitleaks + ruff), `.env.example` | done |
| Research integrity | `research/holdout.lock`, `pmcore/data/holdout.py` (every loader refuses rows at or after 2026-06-23 and logs to `research/holdout_refusals.log`), `pmcore/ledger/runs.py`, `docs/PREREGISTRATION.md` v1, `docs/ASSUMPTIONS.md` | done, prereg unsigned |
| Money and fees | `pmcore/money.py` (integer ticks, 1 cent = 100 ticks), `pmcore/fees/kalshi.py` (exact per-order round-up, regime lookup by date) | done, tested |
| Venue clients | `pmcore/venues/http.py` (TLS always verified, retries, rate limit), `pmcore/venues/kalshi/public.py`, `pmcore/venues/polymarket/{gamma,data_api}.py` (read-only, no auth code) | done, endpoints unverified (ASSUMPTIONS K-API-*, P-API-*) |
| Normalizers | `pmcore/venues/kalshi/normalize.py`, `pmcore/venues/polymarket/normalize.py` | done, tested on synthetic records |
| Lake | `pmcore/lake/parquet.py` (venue/dataset/date/market partitions, DuckDB and Polars) | done, tested |
| Loaders | `scripts/download_kalshi.py`, `scripts/download_polymarket.py` (resumable, checkpointed, holdout-guarded, save first raw responses to `docs/observed/`) | written, never run (NET-1) |
| Reachability probe | `scripts/probe_reachability.py` (stdlib only) | written, Oscar runs it |
| Fair value and stats | `strategies/ref_mm/research/fv.py` (vectorized, exact half-ticks), `pmcore/stats.py` (block bootstrap by event, MDE) | done, tested |
| KATs | `strategies/ref_mm/research/kat/{common,kat1,kat2,kat3}.py` | written, not run (no data); KAT-1 refuses PASS until paper params are verified |
| Mapping | `strategies/ref_mm/mapping/{text,sides,resolution,llm_verify,pipeline}.py` | done, tested on synthetic pairs; never run on real markets |
| Review page | `strategies/ref_mm/review/build_review_page.py` | done, rendered and checked in light and dark mode |
| Legacy fixes | `engine.py` TLS bypass removed (rule 7) | done; the rest of the legacy code is frozen reference |

Tests: 50 passing (`pytest`), `mypy --strict pmcore` clean, ruff clean.

## 2. Numbers

None. No market data has been downloaded, so KAT-1, KAT-2, and KAT-3 have not run and
`research/runs.jsonl` has no entries. Reporting a number here would violate rule 13's
"numbers" requirement in spirit: there is nothing to report yet.

## 3. What was verified and what is OPEN

`docs/ASSUMPTIONS.md` now has 34 rows VERIFIED or OBSERVED and 2 OPEN. The facts that changed
the plan:

- Kalshi's historical cutoff is 2026-07-25 for every data type, so the whole in-sample window
  is served by the `/historical/*` namespace, on both hosts (K-API-2, K-API-9).
- `/historical/markets` has no time filter and exactly one filter at a time; `series_ticker`
  returns only KX-era tickers, so legacy 2021 to early-2025 markets are reachable only by
  sweeping the archive. 83% of the archive by count is hourly crypto and, without the
  multivariate filter, 99% is combo markets. The loader sweeps with `mve_filter=exclude` and
  keeps markets per PREREGISTRATION D7 (K-API-10).
- Public unauthenticated reads are throttled around 1 request per second (429s observed at 2
  per second). An account key would give the Basic tier's Read bucket of 20 reads per second
  (K-API-3). Read-only request signing is implemented and dormant until a key is present.
- Kalshi trades carry `taker_outcome_side` (canonical), `taker_side` (deprecated alias),
  `taker_book_side`, `count_fp`, dollar-string prices, and `is_block_trade` (K-API-7).
- 1-minute candles are retained for archived 2024 markets, sparse (only minutes with book
  changes), at most 5,000 per request (K-API-8, K-API-12).
- Fee schedule verified from Kalshi's own PDFs (archived copies, since kalshi.com blocks this
  session): taker round up(0.07 * C * P * (1 - P)), maker round up(0.0175 * C * P * (1 - P))
  on listed series only; no maker fees anywhere before 2025-05-13; lists expanded 2025-09-18;
  API fee-change feed from 2025-10-04 (K-FEE-1, K-FEE-2, K-FEE-7). Four fee types exist on
  the API; perps and flat schedules are refused by the code (K-FEE-5).
- Polymarket Data API v2 (2026-09-04) pages trades by seek cursor with no cap (40,000 rows in
  20 s); v1's offset cap of 10,000 is real. Gamma has a keyset endpoint and rejects offsets
  past about 9,500 (P-API-1, P-API-2). The condition shape serves a fixed three-year window
  and ignores start/end (P-API-3).
- Polymarket Fee Structure V2 on 2026-03-30 and CLOB V2 on 2026-04-28 are both confirmed
  from Polymarket's changelog (P-FEE-1, P-CLOB-1).
- The KAT-1 paper is on disk. Its method is per market-observation (last trade before close
  plus daily lookbacks), equal-weighted, with fees imputed at 0.07 on 100-contract lots for
  both sides; headline makers -11.99%, takers -31.46% (KAT-1). The harness was rewritten to
  match, and the frozen method is in PREREGISTRATION section 9a.

OPEN: NET-2 from Oscar's machine and us-east-1; P-CHAIN-1 (which on-chain source, now only a
completeness check); K-FEE-6 maker semantics of fee waivers.

## 4. Decisions Oscar must make before the pre-registration is signed

From `docs/PREREGISTRATION.md` section 10, defaults in force until changed:
D1 NO-token prints converted to YES terms; D2 exact charged fee with per-order round-up;
D3 missing FV(t + h) excluded and reported; D4 contract-weighted primary mean; D5 Kalshi
category taxonomy; D6 1/100-cent ticks.

Plus the environment questions from the approved plan: where loaders and the lake run, the
AWS account timing, FRED key, on-chain source choice, the KAT-1 PDF under `docs/refs/`.

## 5. Gate status

| Gate item | Status |
|---|---|
| Polymarket Global public REST and WebSocket reachable from a US IP with no circumvention | PASS from the cloud container (US, Ohio). Pending from Oscar's machine and us-east-1: run `python3 scripts/probe_reachability.py > probe_$(hostname).json` |
| KAT-1 within 3 points of the paper, makers > takers | NOT RUN: trades sweep waiting on the archive sweep |
| KAT-2 favorite-longshot pattern | NOT RUN |
| KAT-3 per-pair correlation and gap, flagged list | NOT RUN: mapping waits on both metadata downloads |
| 100-pair stratified review with zero errors | NOT STARTED |

Data status at the time of writing: Polymarket closed-market metadata 418,784 markets through
2026-02-19 (of the window ending 2026-06-22); Kalshi archive sweep 230 pages, 36,283 markets
kept of 230,000 seen, oldest close 2026-07-20, at the unauthenticated rate.

## 6. Workflow runs (rule 12)

- `phase0-bias-audit` (run id wf_247ab9a4-551, 72 agents, 59 minutes): four adversarial
  lenses (look-ahead, side-inversion, fee math, selection bias) over the pre-registration and
  the core research code, each finding refuted by two independent skeptics. Results and fixes
  in section 7.
- Independent re-derivation of KAT numbers: not run, no numbers to re-derive.

## 7. Bias-audit findings (workflow run wf_247ab9a4-551)

72 agents: 4 finders (look-ahead, side inversion, fee math, selection bias), then two
independent skeptics per finding instructed to refute. 34 findings, 20 survived, 14 refuted.
Every surviving finding was fixed in the same day and the fix is in the commit history.

| # | Lens | Finding (short) | Severity after refutation | Fix |
|---|---|---|---|---|
| C1, C17, C14 | look-ahead, selection | Candidate blocking, the date-window check, the loader's market window, and exclusion 2 used Kalshi `close_time`, which moves earlier when an outcome is known: outcome-dependent universe selection | high | Every date now uses `expected_expiration_time` (fallback `expiration_time`); fields frozen in PREREGISTRATION 5 and 5a |
| C2, C13 | look-ahead, selection | Settlement result written to candidate rows and shown to the reviewer who decides the universe | low to medium | Result removed from rows and page; rejection requires a rules-based reason code; page states outcomes are hidden |
| C3 | look-ahead | Markets checkpoint stored holdout-dated rows without refusal or logging | high | Loader refuses rows whose scheduled end, settlement, or close is in the holdout window and logs the count; stored rows cleared and the sweep resumed |
| C4 | look-ahead | An env var could relocate the holdout lock | low | Override honored only under pytest; ledger records root and lock status |
| C5 | look-ahead | Floor-second Polymarket timestamps shrink L_ref to 1 s | low to medium | FV cutoff widened by one resolution unit; test added |
| C6, C12 | sides, fees | KAT-3 inferred the stored reference token from whichever rows existed; could mask an inversion | high | Every print row carries `stored_reference_token_id`; one shared `in_pair_terms` helper; disagreement raises |
| C7 | sides | Lake column named as if in YES terms | low | Docstring and column semantics fixed with the explicit reference column |
| C8 | sides | Yes/No alignment ignored `yes_sub_title`, so the LLM was the only guard against an inverted binary pair | high | Polarity, negation, and content of the sub-title checked; test "No change" vs "cut" |
| C9 | sides | Prereg section 2 reproduces (no defect) | none | Permanent tests pinned to the worked examples |
| C10 | fees | Fractional counts used the unrounded rate | low to medium | Ceiling applied to fractional counts |
| C11 | fees | Rounding unit (per print vs per order) unstated | medium | Stated in PREREGISTRATION 2.4 with the lower-bound sensitivity |
| C15, C16 | selection | Pair universe not frozen; LLM model, limits, and the reviewed set undefined | medium | Section 5a freezes parameters and defines the universe; mapping runs write parameters and hashes to the ledger |
| C18 | selection | Event-level blocks ignore same-day dependence | low | Two block levels, gate on the coarser, series floor of 10 |
| C19 | selection | H2 had no test statistic | low | Pre-registered difference with CI |
| C20 | selection | Gate H unspecified | low to medium | Tier 1 toxic rule, 200 fills, Tier 0 sign check |

Refuted (kept for the record, no change): a holdout guard on the lake read path (loaders never
write holdout rows), KAT-1 fee regimes (the paper's method imputes 0.07 for both sides), a
"contract-weighted" KAT-1 mean (it is equal-weighted per observation as the paper does),
two-decimal truncation of sizes (that is the venues' own precision), the Decimal fee quotient,
the KAT-3 overlay (pre-registered and shown only for flagged pairs), the one-to-one rule (now
in 5a), truncated Polymarket tapes (v2 has no cap), multiple comparisons across variants (the
selection rule is a single pre-registered choice), the power rule wording, Gate 0 wording, and
the bootstrap defaults (now asserted at gate time).

## 8. What failed or was cut

- The KAT-1 replication cannot be exact without the paper. The harness parameterizes the
  window, universe, and return definition and refuses to report PASS until
  `paper_params_verified` is true.
- The spec's worked fee examples use the unrounded per-contract rate; Kalshi charges a
  per-order ceiling. The pre-registration restates the examples under D2. Oscar decides.
- Candidate generation uses token overlap plus end-date proximity, not TF-IDF, to avoid a
  scikit-learn dependency in Phase 0. It is a recall lever only; every accept still passes
  side alignment, the deterministic checks, the LLM pass, and one-to-one enforcement.

## 9. Next steps

1. Oscar: a Kalshi API key in `.env` (read-only use, 20x the download rate); the probe from
   his machine and us-east-1; decisions D1 to D10.
2. Let the Kalshi archive sweep reach 2021-06, then run the global trades sweep.
3. Run KAT-1 and KAT-2; if either fails, stop.
4. Run the mapping pipeline over kept Kalshi markets and Polymarket closed markets, download
   v2 trades and 1-minute candles for candidates, run KAT-3, build the review page, hand Oscar
   the 100-pair sample.
5. Independent re-derivation workflow for the KAT numbers; re-issue this report with numbers
   and the signed pre-registration hash.
