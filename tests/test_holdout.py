from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pmcore.data import holdout


def _ts(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def test_locked_refuses_and_logs(tmp_repo: Path) -> None:
    (tmp_repo / "research" / "holdout.lock").write_text("locked")
    assert holdout.is_locked()
    holdout.guard_timestamp(_ts(2026, 6, 22), "t")  # last in-sample day is fine
    with pytest.raises(holdout.HoldoutRefused):
        holdout.guard_timestamp(_ts(2026, 6, 23), "test_source")
    log = (tmp_repo / "research" / "holdout_refusals.log").read_text().strip().splitlines()
    assert len(log) == 1
    rec = json.loads(log[0])
    assert rec["source"] == "test_source" and rec["count"] == 1


def test_filter_rows_counts(tmp_repo: Path) -> None:
    (tmp_repo / "research" / "holdout.lock").write_text("locked")
    rows = [_ts(2026, 6, 1), _ts(2026, 6, 23), _ts(2026, 8, 1)]
    kept = holdout.filter_rows(rows, lambda r: r, "bulk")
    assert kept == [_ts(2026, 6, 1)]
    rec = json.loads((tmp_repo / "research" / "holdout_refusals.log").read_text().splitlines()[0])
    assert rec["count"] == 2
    assert holdout.in_sample_end_exclusive() == holdout.HOLDOUT_START


def test_unlocked_allows(tmp_repo: Path) -> None:
    assert not holdout.is_locked()
    holdout.guard_timestamp(_ts(2026, 7, 1), "t")
    assert holdout.in_sample_end_exclusive() == holdout.HOLDOUT_END
    assert not (tmp_repo / "research" / "holdout_refusals.log").exists()


def test_naive_timestamp_rejected(tmp_repo: Path) -> None:
    with pytest.raises(ValueError):
        holdout.guard_timestamp(datetime(2026, 1, 1), "t")


def test_real_repo_lock_exists() -> None:
    # The real lock must exist in Phase 0 (spec section 4). Uses the real repo root.
    real = Path(__file__).resolve().parents[1] / "research" / "holdout.lock"
    assert real.exists()
