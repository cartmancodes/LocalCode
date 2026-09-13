from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["claude", "codex", "fleet", "opencode"]

# The same names, as data. Two validators below used to carry their own copies
# of this tuple, so adding a provider meant editing the literal in three places
# and silently rejecting the new name in whichever one was missed.
PROVIDERS: tuple[str, ...] = ("claude", "codex", "fleet", "opencode")


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
        default="claude:claude-sonnet-4-6,codex:gpt-5.3-codex,opencode:gpt-4o-mini",
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

    # How many long-lived sub-provider worker PROCESSES the fleet may hold at
    # once. Each one is an interpreter with the SDK imported and (while a step
    # runs) a vendor CLI under it, so this is the bound on "a burst of
    # concurrent turns swapped the machine". Past the cap the least recently
    # used IDLE worker is closed; a worker with a step in flight is never
    # evicted. Env: FLEET_MAX_WORKERS.
    fleet_max_workers: int = 4
    # A worker idle for this long is reaped. Long enough that the steps of one
    # turn — and a user's next prompt a minute later — reuse the process that
    # already paid the interpreter + SDK import; short enough that a session
    # left open overnight is not holding four interpreters.
    # Env: FLEET_WORKER_IDLE_S.
    fleet_worker_idle_s: float = 300.0
    # Per-TURN token ceiling across every sub-agent dispatch, summed from each
    # step's reported usage. 0 disables it. Above the ceiling
    # ``dispatch_subagent`` refuses and tells the orchestrator to stop and
    # summarize — the bound on a turn that keeps delegating instead of
    # finishing. Env: FLEET_TURN_TOKEN_BUDGET.
    fleet_turn_token_budget: int = 0

    # Comma-separated CORS origins. Override per env (e.g. add a staging URL).
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Comma-separated absolute directory roots that are valid `cwd` values for a
    # session. Default-deny. A session whose cwd is outside every root is
    # rejected with HTTP 400. "~" means "anywhere under the user's home" —
    # broad enough for real work, narrow enough that "/" and "/etc" are
    # refused. Mitigates path traversal via spawned subprocesses.
    allowed_cwd_roots: str = "~"
    # Always refused even when under an allowed root. These hold the very
    # credentials the invariant exists to protect.
    denied_cwd_paths: str = (
        "~/.ssh,~/.aws,~/.gnupg,~/.config/gh,~/.claude,~/.codex,"
        "~/.local/share/opencode,~/Library/Keychains"
    )
    # bypassPermissions disables every tool gate in the spawned CLI. Off
    # unless the operator explicitly opts in. Env: ALLOW_BYPASS_PERMISSIONS.
    allow_bypass_permissions: bool = False
    # How long a tool-approval card waits for the user before it is denied.
    tool_approval_timeout_s: float = 300.0

    # How many live `claude` CLI clients the Claude provider may hold at once.
    # Each one is a connected `ClaudeSDKClient`, and behind it a real `claude`
    # subprocess with its own memory — so this is the bound on "the user left
    # forty sessions open and the machine swapped". Past the cap the least
    # recently used client is disconnected; its session is not lost, the next
    # turn there reconnects and resumes. Env: CLAUDE_MAX_LIVE_CLIENTS.
    claude_max_live_clients: int = 8

    # The Codex CLI, spawned as `codex app-server`. A NAME, not a path, and not
    # a credential: the binary finds its own OAuth token (`codex login`) and
    # LocalCode never holds one — see backend/app/invariants.py. Overridable so
    # a test can point it at a stand-in and a user at a non-PATH install.
    # Env: CODEX_BINARY.
    codex_binary: str = "codex"
    # Spawn plus handshake. Generous because a cold `codex app-server` on a
    # large repo is not instant, bounded because a wedged one must surface as
    # an error rather than a turn that never starts.
    codex_startup_timeout_s: float = 30.0
    # Per-request ceiling once the server is up (thread/start, turn/start).
    # Long, because `turn/start` is answered only when the turn is accepted.
    codex_request_timeout_s: float = 120.0

    # Bound on per-session lock map and per-message pagination caps.
    messages_page_default: int = 50
    messages_page_max: int = 500

    # A checkpoint exists so a crash does not lose the whole turn. One per tool
    # boundary is more often than that needs, and on a tool-heavy turn the
    # writes dominate. Throttle by both time and growth; a final checkpoint
    # always writes regardless.
    #
    # The growth arm is amortized against the last checkpoint's size (see
    # ``TurnAccumulator._should_write``): a fixed byte threshold alone still
    # rewrites a growing snapshot O(size/threshold) times, which is the
    # quadratic write volume this throttle exists to remove.
    #
    # What a crash (or a mid-turn reload) therefore loses — the bound to tune
    # on, and it is NOT ``checkpoint_min_interval_s``:
    #   * while the message is under ``checkpoint_min_growth_bytes``, at most
    #     one interval's worth of work;
    #   * above that floor the time arm no longer applies at all, and the next
    #     checkpoint waits for the message to grow by as much as the last one
    #     wrote. The unsaved tail is bounded by the last checkpoint's size —
    #     roughly half the message so far — with NO time ceiling. A turn
    #     sitting at 1 MB that grows slowly can go many minutes without a
    #     checkpoint.
    # Lowering ``checkpoint_min_growth_bytes`` tightens the bound only while
    # the message is small; above the floor the tail is ~half the message
    # whatever these are set to, because that is what amortizing a
    # whole-message rewrite means. Tightening it there costs write volume
    # proportional to how often you rewrite — the quadratic behaviour this
    # replaced.
    checkpoint_min_interval_s: float = 2.0
    checkpoint_min_growth_bytes: int = 64 * 1024

    # Above this many UTF-8 bytes, a step/tool output goes to the artifact
    # store instead of straight into context — a 2 MB test log inlined into
    # the transcript would blow the context window and, worse, change the
    # prompt prefix for every turn after it (cold cache for the rest of the
    # session). Env: ARTIFACT_INLINE_MAX_BYTES.
    artifact_inline_max_bytes: int = 8_000
    # Override where large outputs are content-addressed. Empty string means
    # "under the user's home", resolved lazily by ArtifactStore itself so a
    # test that redirects HOME still lands under the fixture's throwaway
    # directory instead of the developer's real one.
    artifact_root: str = ""

    # Override where turn usage is appended. ``None`` means
    # ``~/.localcode/usage.jsonl``, resolved lazily by ``UsageLog`` itself —
    # a production object (``ClaudeProvider``) built inside a test must read
    # this setting rather than reaching straight for ``Path.home()``, or a
    # test that forgets to redirect HOME (or constructs the provider before
    # HOME is redirected) silently appends to the developer's real log.
    # Env: USAGE_LOG_PATH.
    usage_log_path: str | None = None

    @field_validator("default_provider")
    @classmethod
    def _validate_default_provider(cls, v: str) -> str:
        if v not in PROVIDERS:
            raise ValueError(f"default_provider must be one of {', '.join(PROVIDERS)}")
        return v

    def catalog(self) -> list[CatalogEntry]:
        entries: list[CatalogEntry] = []
        for raw in self.model_catalog.split(","):
            raw = raw.strip()
            if not raw:
                continue
            provider, _, model = raw.partition(":")
            if provider not in PROVIDERS or not model:
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

    def denied_path_list(self) -> list[Path]:
        return [
            Path(p).expanduser().resolve()
            for p in self.denied_cwd_paths.split(",")
            if p.strip()
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
