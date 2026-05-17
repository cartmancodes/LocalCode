from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


Provider = Literal["claude", "opencode", "fleet"]


class CatalogEntry:
    __slots__ = ("provider", "model")

    def __init__(self, provider: Provider, model: str) -> None:
        self.provider = provider
        self.model = model

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.model}"

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "provider": self.provider, "model": self.model}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "LocalCode Orchestrator"
    env: str = "dev"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    opencode_base_url: str = "http://localhost:4096"

    # How long before a session is considered stale and gets swept by the
    # cleanup pass. Mirrors Claude Code's ``cleanupPeriodDays``. Set to 0 to
    # disable auto-deletion entirely (sessions accumulate forever; manual
    # cleanup via the UI's /clear-all only).
    session_retention_days: int = 7

    default_provider: Provider = "claude"
    default_model: str = "claude-sonnet-4-6"
    model_catalog: str = Field(
        default="claude:claude-sonnet-4-6,opencode:gpt-4o-mini",
        description="Comma-separated provider:model entries.",
    )

    # Override the fleet config search path. Pulled through Settings (not read
    # raw via os.environ at request time) so behavior matches the rest of the
    # config — predictable rather than secretly hot-reloading.
    localcode_fleet_config: str | None = None

    # Per sub-provider STEP budget (seconds). A claude-opus planner auditing
    # a large repo with tools legitimately runs many minutes, so the ceiling
    # is generous but bounded. Env: FLEET_STEP_TIMEOUT_S.
    fleet_step_timeout_s: float = 1200.0
    # If a sub-provider emits ZERO events within this window it's treated as
    # wedged (auth prompt / dead socket) and fast-failed — distinct from
    # "slow but streaming". Env: FLEET_STARTUP_GRACE_S.
    fleet_startup_grace_s: float = 90.0

    # Comma-separated CORS origins. Override per env (e.g. add a staging URL).
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Comma-separated absolute directory roots that are valid `cwd` values for a
    # session. Empty = no allowlist (permissive — fine for local dev). When set,
    # any session-creation request with a `cwd` not under one of these roots is
    # rejected with HTTP 400. Mitigates path traversal via spawned subprocesses.
    allowed_cwd_roots: str = ""

    # Bound on per-session lock map and per-message pagination caps.
    messages_page_default: int = 50
    messages_page_max: int = 500

    @field_validator("default_provider")
    @classmethod
    def _validate_default_provider(cls, v: str) -> str:
        if v not in ("claude", "opencode", "fleet"):
            raise ValueError("default_provider must be 'claude', 'opencode', or 'fleet'")
        return v

    def catalog(self) -> list[CatalogEntry]:
        entries: list[CatalogEntry] = []
        for raw in self.model_catalog.split(","):
            raw = raw.strip()
            if not raw:
                continue
            provider, _, model = raw.partition(":")
            if provider not in ("claude", "opencode", "fleet") or not model:
                # Skip malformed entries silently — surfacing them would block startup.
                continue
            entries.append(CatalogEntry(provider, model))  # type: ignore[arg-type]
        return entries

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def cwd_allowlist(self) -> list[Path]:
        return [
            Path(p).expanduser().resolve()
            for p in self.allowed_cwd_roots.split(",")
            if p.strip()
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
