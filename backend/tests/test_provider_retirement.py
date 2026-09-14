"""Configs that name a provider which no longer exists.

`opencode` was removed. Configs naming it are still on disk in people's
projects and in old sessions, so the failure modes worth pinning are the ones a
user hits *after* upgrading, not the removal itself:

  * a fleet role naming the retired provider has to fall back on the provider
    **and its model together**. A model name is only meaningful to the provider
    it was written for — falling back to `claude` while keeping
    `openai/gpt-5.3-codex` produces `claude:openai/gpt-5.3-codex`, which
    resolves nowhere and fails at dispatch with a message about the model
    rather than about the config;
  * the API boundary has to reject it outright rather than accept a session it
    cannot run;
  * the registry has to refuse it by name.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from backend.app.orchestrator import registry
from backend.app.orchestrator.fleet import (
    ROLE_LIBRARY,
    VALID_PROVIDERS,
    load_fleet_config,
)
from backend.app.schemas import CreateSessionRequest

RETIRED = "opencode"


def test_the_retired_provider_is_gone_from_the_vocabulary() -> None:
    assert RETIRED not in VALID_PROVIDERS
    assert set(VALID_PROVIDERS) == {"claude", "codex"}


def test_a_retired_role_falls_back_on_the_provider_and_its_model(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    config = tmp_path / ".localcode" / "fleet.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "name: legacy\n"
        "roles:\n"
        f"  coder: {{ provider: {RETIRED}, model: openai/gpt-5.3-codex }}\n"
        "  reviewer: { provider: claude, model: claude-haiku-4-5 }\n"
    )

    with caplog.at_level(logging.WARNING):
        cfg = load_fleet_config(str(tmp_path))

    coder = cfg.roles["coder"]
    assert coder.provider in VALID_PROVIDERS
    assert coder.model != "openai/gpt-5.3-codex", (
        "the retired provider's model must not survive the fallback"
    )
    assert coder.model == ROLE_LIBRARY["coder"].model
    # a role that was already valid is left exactly as written
    assert cfg.roles["reviewer"].provider == "claude"
    assert cfg.roles["reviewer"].model == "claude-haiku-4-5"
    # and the warning says which pair moved, not just that something was wrong
    assert RETIRED in caplog.text
    assert ROLE_LIBRARY["coder"].model in caplog.text


def test_the_api_boundary_rejects_it() -> None:
    with pytest.raises(ValidationError) as excinfo:
        CreateSessionRequest(provider=RETIRED, model="openai/gpt-5.3-codex")
    assert RETIRED in str(excinfo.value)


async def test_the_registry_refuses_it_by_name() -> None:
    with pytest.raises(ValueError, match=RETIRED):
        await registry.get_provider(RETIRED)  # type: ignore[arg-type]


def test_its_module_is_actually_gone() -> None:
    """A stale .pyc or an editor's autosave would otherwise keep it importable."""
    with pytest.raises(ModuleNotFoundError):
        __import__("backend.app.orchestrator.opencode")
