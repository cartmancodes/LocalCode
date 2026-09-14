from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``backend.app`` importable when pytest is run from the repo root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def agent_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user-global agent dir at a temp dir so tests never touch
    ``~/.localcode``."""
    d = tmp_path / "agent"
    d.mkdir()
    monkeypatch.setenv("LOCALCODE_AGENT_DIR", str(d))
    return d


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p
