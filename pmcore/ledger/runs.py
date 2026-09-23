"""Runs ledger (spec section 4): append every analysis run to research/runs.jsonl."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pmcore.data.holdout import repo_root


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()[:16]


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def git_dirty() -> bool:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return True


def prereg_hash() -> str:
    p = repo_root() / "docs" / "PREREGISTRATION.md"
    if not p.exists():
        return "missing"
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def ledger_path() -> Path:
    return repo_root() / "research" / "runs.jsonl"


def record_run(
    name: str,
    config: dict[str, Any],
    window: tuple[str, str],
    metrics: dict[str, Any],
    notes: str = "",
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "name": name,
        "config_hash": config_hash(config),
        "config": config,
        "code_commit": git_commit(),
        "code_dirty": git_dirty(),
        "prereg_hash": prereg_hash(),
        "repo_root": str(repo_root()),
        "holdout_locked": (repo_root() / "research" / "holdout.lock").exists(),
        "window": {"start": window[0], "end": window[1]},
        "metrics": metrics,
        "notes": notes,
    }
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(canonical_json(rec) + "\n")
    return rec
