# CLAUDE.md

Project: ref_mm, a Kalshi maker strategy priced off Polymarket Global fair value. The full
specification is `docs/SPEC_ref_mm.md` and is authoritative. This file holds the standing
rules and the repo conventions every session must follow.

## Standing rules (from the spec, verbatim)

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

## Repo conventions

- Layout: `pmcore/` (venue clients, money, fees, lake, ledger, config, risk), `strategies/ref_mm/`
  (research/tier0, tier1, tier2, kat; mapping; sim; live), `scripts/` (resumable downloaders and
  probes), `research/` (holdout.lock, runs.jsonl, refusal log), `docs/` (spec, assumptions,
  pre-registration, reports), `tests/`.
- Python 3.12 target (`pyproject.toml`). Tooling: `uv` or `pip`, `ruff`, `mypy --strict pmcore`,
  `pytest` with `hypothesis`.
- Prices are stored as integer ticks: 1 dollar = 10000 ticks, 1 cent = 100 ticks. Use
  `pmcore.money`. Fees use `Decimal` and Kalshi's exact round-up-to-cent rule. No `float` on
  any price, size, fee, or P&L path. Floats are allowed only in statistics on already-computed
  per-contract returns, and must be labeled as such.
- Windows: in-sample 2024-01-01 to 2026-06-22, holdout 2026-06-23 to 2026-09-22. While
  `research/holdout.lock` exists every loader calls `pmcore.data.holdout.guard()` and refuses
  rows at or after 2026-06-23, logging each refusal to `research/holdout_refusals.log`.
- Every analysis run appends to `research/runs.jsonl` via `pmcore.ledger.runs`.
- `docs/ASSUMPTIONS.md` lists each external fact as VERIFIED (URL, date) or OPEN (owner). Code
  that depends on an OPEN fact says so in a comment naming the assumption id.
- The legacy arbitrage code (`engine.py`, `strategy*_resolver.py`, `merge_and_verify.py`) is
  frozen reference material. It violates rules 5 and 7 and derives Polymarket sides from token
  index. Do not import from it; port logic into `pmcore/` or `strategies/ref_mm/mapping/` with
  tests.
- Secrets: `.env` locally (git-ignored, see `.env.example`), SSM in production. `gitleaks` runs
  as a pre-commit hook. Never print a key.
- Kalshi is the only venue that ever receives an order. There is no Polymarket CLOB
  authentication code in this repo, by design.
- Commits: small, descriptive, no model identifiers in commit messages or code.
