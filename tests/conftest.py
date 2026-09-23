from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point pmcore at a throwaway repo root so tests never touch research/ in the real repo."""
    (tmp_path / "research").mkdir()
    (tmp_path / "docs").mkdir()
    monkeypatch.setenv("REF_MM_REPO_ROOT", str(tmp_path))
    assert os.environ["REF_MM_REPO_ROOT"] == str(tmp_path)
    return tmp_path
