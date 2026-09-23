# PHASE 0 report (foundations, data plumbing, known-answer tests, mapping)

Status: **BLOCKED on environment and inputs from Oscar, code complete for everything that does
not need network access.** Go/no-go on the Phase 0 blocker (US reachability of Polymarket
Global) is **undetermined**: it can only be measured from Oscar's machine and us-east-1.

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

See `docs/ASSUMPTIONS.md` for the full table. Summary:

- OBSERVED: the cloud session's network policy blocks every venue, docs, FRED, and vendor host
  (NET-1). Only pypi and web search work.
- PARTIAL (web-search snippets of official pages, must be re-read on the official page):
  Kalshi maker and taker fee formulas and round-up (K-FEE-1, K-FEE-2), the historical API
  namespace (K-API-9 to 11), Polymarket's 2026-03-30 fee rollout (P-FEE-1), Predexon's
  product shape (PRED-1), the KAT-1 paper's identity and headline numbers (KAT-1).
- OPEN, blocking: which host serves Kalshi `/historical/*` (K-API-2); 1-minute candle
  retention for archived markets (K-API-12); Data API time filters and the offset cap
  (P-API-2, P-API-3); CLOB V2 date (P-CLOB-1); the KAT-1 paper's sample window, universe,
  and return definition (KAT-1); US reachability (NET-2).

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
| Polymarket Global public REST and WebSocket reachable from Oscar's machine and us-east-1 with no circumvention | UNDETERMINED: run `python3 scripts/probe_reachability.py > probe_$(hostname).json` on both and send the files |
| KAT-1 within 3 points of the paper, makers > takers | NOT RUN (no data; paper params unverified) |
| KAT-2 favorite-longshot pattern | NOT RUN |
| KAT-3 per-pair correlation and gap, flagged list | NOT RUN |
| 100-pair stratified review with zero errors | NOT STARTED (no pairs) |

## 6. Workflow runs (rule 12)

- `phase0-bias-audit` (run id wf_247ab9a4-551): four adversarial lenses (look-ahead,
  side-inversion, fee math, selection bias) over the pre-registration and the core research
  code, each finding refuted by two independent skeptics. Results in section 7.
- Independent re-derivation of KAT numbers: not run, no numbers to re-derive.

## 7. Bias-audit findings

(filled in below when the run completes)

## 8. What failed or was cut

- The KAT-1 replication cannot be exact without the paper. The harness parameterizes the
  window, universe, and return definition and refuses to report PASS until
  `paper_params_verified` is true.
- The spec's worked fee examples use the unrounded per-contract rate; Kalshi charges a
  per-order ceiling. The pre-registration restates the examples under D2. Oscar decides.
- Candidate generation uses token overlap plus end-date proximity, not TF-IDF, to avoid a
  scikit-learn dependency in Phase 0. It is a recall lever only; every accept still passes
  side alignment, the deterministic checks, the LLM pass, and one-to-one enforcement.

## 9. Next steps once unblocked

1. Oscar: widen network access or designate the download host; run the probe on both hosts;
   drop the KAT-1 PDF; answer the D1 to D6 decisions; confirm the environment questions.
2. Verify every PARTIAL and OPEN assumption on the official pages and record it.
3. Run `download_kalshi.py --phase fees` then `--phase markets --limit 50` as a smoke test;
   inspect `docs/observed/*.json`; fix the normalizers against observed behavior; then the
   full in-sample download in the background.
4. Run KAT-1 and KAT-2; if either fails, stop.
5. Run the mapping pipeline, KAT-3, build the review page, and hand Oscar the sample.
6. Independent re-derivation workflow for the KAT numbers; then re-issue this report with
   numbers and the signed pre-registration hash.
