"""LLM verification pass (rule 3): schema-validated, cached, auditable, never the only guard.

Posture (rule 1): the default verdict is REJECT. The model must name a concrete reason for
MATCH on every resolution dimension. Any parse failure, API error, refusal, or missing field is
REJECT. The deterministic checks in `resolution.py` and `sides.py` run first; this pass only
sees pairs that passed them or came back UNKNOWN, and its side_alignment answer must agree with
the deterministic one or the pair is rejected.

Cache: data/cache/llm/<sha256(model + system + prompt)>.json. Log: data/llm_verification_log.jsonl.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from pmcore.data.holdout import repo_root

DEFAULT_MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You verify whether a Polymarket contract and a Kalshi contract resolve on the EXACT SAME real-world outcome. This gates a real-money strategy where a false MATCH causes a guaranteed loss and a false REJECT only costs a missed opportunity. Your DEFAULT is REJECT.

For each dimension answer true only if you can point to concrete text on both sides establishing identity:
- same_underlying: the same event, entity, metric, or contest.
- same_threshold: identical numeric thresholds, units, and strict/inclusive bounds (or no thresholds on either side).
- same_date_window: identical deadlines or measurement periods.
- same_source: the same resolution source or authority, or both unspecified in a way that cannot diverge.
- same_tie_handling: identical treatment of ties, postponements, cancellations, and voids.
- side_alignment: "same" if a YES on Kalshi pays exactly when the named Polymarket reference outcome pays; "inverted" if it pays when the OTHER outcome pays; "unknown" otherwise.

verdict MUST be REJECT unless every same_* field is true and side_alignment is "same" or "inverted". Quote the decisive text in `reason`."""


class Verdict(BaseModel):
    poly_resolution: str = Field(description="one sentence: what the Polymarket side resolves on")
    kalshi_resolution: str = Field(description="one sentence: what the Kalshi side resolves on")
    same_underlying: bool
    same_threshold: bool
    same_date_window: bool
    same_source: bool
    same_tie_handling: bool
    side_alignment: Literal["same", "inverted", "unknown"]
    verdict: Literal["MATCH", "REJECT"]
    reason: str


@dataclass(frozen=True, slots=True)
class LlmResult:
    verdict: Verdict | None
    final: Literal["MATCH", "REJECT"]
    cached: bool
    error: str
    cache_key: str


def cache_dir() -> Path:
    d = repo_root() / "data" / "cache" / "llm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    p = repo_root() / "data" / "llm_verification_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def build_prompt(pair: dict[str, str]) -> str:
    return (
        f"POLYMARKET\n  question: {pair.get('poly_question', '')}\n  outcomes: {pair.get('poly_outcomes', '')}\n"
        f"  reference outcome (the one we map to Kalshi YES): {pair.get('reference_outcome', '')}\n"
        f"  end date: {pair.get('poly_end_date', '')}\n  description/rules: {pair.get('poly_description', '')[:2500]}\n\n"
        f"KALSHI\n  title: {pair.get('kalshi_title', '')}\n  yes_sub_title: {pair.get('kalshi_yes_sub_title', '')}\n"
        f"  scheduled end: {pair.get('kalshi_scheduled_end', '')}\n"
        f"  rules_primary: {pair.get('kalshi_rules_primary', '')[:2500]}\n  rules_secondary: {pair.get('kalshi_rules_secondary', '')[:1500]}\n\n"
        f"Deterministic pre-checks: {pair.get('checks_summary', '')}\n"
    )


def _key(model: str, prompt: str) -> str:
    return hashlib.sha256(
        (model + "\n" + SYSTEM_PROMPT + "\n" + prompt).encode("utf-8")
    ).hexdigest()


def _finalize(
    v: Verdict | None, deterministic_side: str | None
) -> tuple[Literal["MATCH", "REJECT"], str]:
    if v is None:
        return "REJECT", "no valid verdict"
    if v.verdict != "MATCH":
        return "REJECT", "model verdict REJECT"
    if not (
        v.same_underlying
        and v.same_threshold
        and v.same_date_window
        and v.same_source
        and v.same_tie_handling
    ):
        return "REJECT", "model said MATCH but a dimension is false (override)"
    if v.side_alignment != "same":
        return (
            "REJECT",
            f"side_alignment {v.side_alignment} is not 'same' (inverted pairs are rejected, never flipped)",
        )
    if deterministic_side is not None and deterministic_side != "same":
        return "REJECT", "deterministic side alignment disagrees"
    return "MATCH", "all dimensions true and side alignment same"


def verify_pair(
    pair: dict[str, str],
    *,
    model: str | None = None,
    deterministic_side: str | None = "same",
    offline: bool = False,
) -> LlmResult:
    model = model or os.environ.get("REF_MM_LLM_MODEL", DEFAULT_MODEL)
    prompt = build_prompt(pair)
    key = _key(model, prompt)
    cache_file = cache_dir() / f"{key}.json"
    verdict: Verdict | None = None
    error = ""
    cached = False
    if cache_file.exists():
        try:
            verdict = Verdict.model_validate_json(cache_file.read_text(encoding="utf-8"))
            cached = True
        except ValidationError as e:
            error = f"cache invalid: {e}"
    elif offline:
        error = "offline: no cached verdict"
    else:
        try:
            import anthropic

            client = anthropic.Anthropic()
            resp = client.messages.parse(
                model=model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_format=Verdict,
            )
            if resp.stop_reason == "refusal":
                error = "refusal"
            else:
                verdict = resp.parsed_output
                if verdict is not None:
                    cache_file.write_text(verdict.model_dump_json(indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - any failure is a REJECT, logged
            error = f"{type(e).__name__}: {str(e)[:300]}"
    final, why = _finalize(verdict, deterministic_side)
    rec = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "cache_key": key,
        "cached": cached,
        "pair_id": pair.get("pair_id", ""),
        "final": final,
        "why": why,
        "error": error,
        "verdict": verdict.model_dump() if verdict else None,
    }
    with log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    return LlmResult(verdict, final, cached, error or why, key)
