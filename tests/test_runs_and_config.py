from __future__ import annotations

import json
from pathlib import Path

import pytest

from pmcore import config
from pmcore.ledger import runs


def test_record_run_appends(tmp_repo: Path) -> None:
    rec = runs.record_run("unit", {"a": 1, "b": [1, 2]}, ("2024-01-01", "2026-06-22"), {"m": 0.5})
    lines = (tmp_repo / "research" / "runs.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["config_hash"] == rec["config_hash"]
    assert rec["config_hash"] == runs.config_hash({"b": [1, 2], "a": 1})  # key order irrelevant
    assert rec["prereg_hash"] == "missing"


def test_paper_is_default(tmp_path: Path) -> None:
    assert config.resolve_mode(False, None) == "paper"
    assert config.resolve_mode(False, tmp_path / "nope.toml") == "paper"


def test_live_requires_both(tmp_path: Path) -> None:
    with pytest.raises(config.ArmingError):
        config.resolve_mode(True, None)
    cfg = tmp_path / "live.toml"
    cfg.write_text("live_armed = false\n")
    with pytest.raises(config.ArmingError):
        config.resolve_mode(True, cfg)
    cfg.write_text("live_armed = true\n")
    assert config.resolve_mode(True, cfg) == "live"
    assert config.resolve_mode(False, cfg) == "paper"
