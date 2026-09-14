from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.eval import config


def test_named_configs_are_the_matrix_cells():
    cell = config.EvalConfig.named("claude-single")
    assert cell.provider_name == "claude"
    assert cell.fleet_override is None
    fleet = config.EvalConfig.named("claude-fleet")
    assert fleet.provider_name == "fleet"
    assert fleet.fleet_override is not None
    assert "claude" in fleet.governed_providers()
    with pytest.raises(KeyError):
        config.EvalConfig.named("nope")


def test_eval_root_is_under_the_redirected_home(tmp_path: Path):
    root = config.eval_root()
    assert root == Path.home() / ".localcode" / "eval"
    assert str(root).startswith(str(tmp_path))


def test_spec_constants_are_verbatim():
    assert config.EVAL_HEADROOM_FLOOR == 0.15
    assert config.EVAL_TURN_TIMEOUT_S == 1200.0
    assert config.EVAL_TURN_TOKEN_BUDGET == 400_000
    assert config.EVAL_TURN_MAX_TOOL_CALLS == 200
    assert config.EVAL_MAX_WAIT_S == 6 * 3600.0
    assert config.PROMPT_VERSION == 1
    assert config.DATASET_NAME == "SWE-bench/SWE-bench_Verified"
