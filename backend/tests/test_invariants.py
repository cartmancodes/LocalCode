"""Architecture invariants — the rules that keep both subscriptions usable.

1. The shell never touches a vendor credential store. Auth belongs to the
   official binaries (`claude login`, `codex login`). Pi is the cautionary
   tale: it forwarded the Claude OAuth token itself and lost plan access.
2. The shell never turns a stored token into an API key for a vendor SDK.
"""

from __future__ import annotations

import re
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "app" / "core"

FORBIDDEN_PATTERNS = [
    # credential stores
    r"\.credentials\.json",
    r"\.claude/\.credentials",
    r"auth\.json",
    r"\.codex/auth",
    r"keychain",
    r"security\s+find-generic-password",
    # token forwarding / spoofing
    r"ANTHROPIC_API_KEY\s*[:=]",
    r"OPENAI_API_KEY\s*[:=]",
    r"Authorization[\"']?\s*[:=]\s*[\"']?Bearer",
    r"x-api-key",
    r"anthropic-beta",
    r"chatgpt-account-id",
    r"oauth_token|refresh_token|access_token",
]

ALLOWED_MENTIONS = {
    # the Codex engine *refuses* the token-refresh request — the mention is the guard itself
    "engines/codex.py": [r"account/chatgptAuthTokens/refresh", r"never holds"],
}


def _sources() -> list[Path]:
    return sorted(p for p in CORE.rglob("*.py"))


def test_core_never_reads_credential_stores_or_forwards_tokens() -> None:
    offenders: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        code_lines = [
            line
            for line in text.splitlines()
            if not line.strip().startswith("#") and not line.strip().startswith(('"""', "'''"))
        ]
        code = "\n".join(code_lines)
        for pattern in FORBIDDEN_PATTERNS:
            for match in re.finditer(pattern, code, flags=re.I):
                snippet = code[max(0, match.start() - 40) : match.end() + 40].replace("\n", " ")
                offenders.append(f"{path.relative_to(CORE)}: /{pattern}/ near …{snippet}…")
    assert not offenders, "credential access or token forwarding in core:\n" + "\n".join(offenders)


def test_codex_engine_refuses_token_refresh_requests() -> None:
    text = (CORE / "engines" / "codex.py").read_text(encoding="utf-8")
    assert "account/chatgptAuthTokens/refresh" in text
    assert "never holds" in text


def test_engines_spawn_official_binaries_only() -> None:
    codex = (CORE / "engines" / "codex_rpc.py").read_text(encoding="utf-8")
    assert '["codex", "app-server"]' in codex
    claude = (CORE / "engines" / "claude.py").read_text(encoding="utf-8")
    assert "ClaudeSDKClient" in claude
    # no direct HTTP client to a vendor API anywhere in core
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        assert "api.anthropic.com" not in text, path
        assert "api.openai.com" not in text, path
        assert "chatgpt.com/backend-api" not in text, path
