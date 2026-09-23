from __future__ import annotations

from pathlib import Path

from strategies.ref_mm.mapping import llm_verify as lv


def test_offline_defaults_to_reject_and_logs(tmp_repo: Path) -> None:
    r = lv.verify_pair({"pair_id": "x", "poly_question": "q"}, offline=True)
    assert r.final == "REJECT" and not r.cached
    assert (tmp_repo / "data" / "llm_verification_log.jsonl").exists()


def test_cached_verdict_used_and_overrides(tmp_repo: Path) -> None:
    pair = {"pair_id": "y", "poly_question": "q"}
    key = lv._key(lv.DEFAULT_MODEL, lv.build_prompt(pair))
    good = lv.Verdict(
        poly_resolution="a",
        kalshi_resolution="b",
        same_underlying=True,
        same_threshold=True,
        same_date_window=True,
        same_source=True,
        same_tie_handling=True,
        side_alignment="same",
        verdict="MATCH",
        reason="r",
    )
    (lv.cache_dir() / f"{key}.json").write_text(good.model_dump_json())
    r = lv.verify_pair(pair, offline=True)
    assert r.final == "MATCH" and r.cached
    # inverted sides are rejected, never flipped
    inv = good.model_copy(update={"side_alignment": "inverted"})
    (lv.cache_dir() / f"{key}.json").write_text(inv.model_dump_json())
    assert lv.verify_pair(pair, offline=True).final == "REJECT"
    # MATCH with a false dimension is overridden
    bad = good.model_copy(update={"same_source": False})
    (lv.cache_dir() / f"{key}.json").write_text(bad.model_dump_json())
    assert lv.verify_pair(pair, offline=True).final == "REJECT"
