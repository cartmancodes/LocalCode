# Backend Code Review — Concurrency, Correctness & Performance

A senior-Python-developer pass over the backend at commit ~`2026-05-09`. Findings are ranked by severity. Each entry has a root cause and a concrete fix; nothing here is a stylistic nit.

> **Status as of 2026-05-09 (post-fix pass):** 20 of 21 findings are **resolved**. The one deliberate skip is **#12 (parallelize fleet steps)** — implementing it correctly requires explicit cross-step file-state declarations from the planner, which we don't yet have; the speed gain isn't worth the silent-corruption risk. Re-evaluate when adding a real workflow engine.

**Files reviewed:**
- [backend/app/routes/sessions.py](../backend/app/routes/sessions.py)
- [backend/app/orchestrator/{base,claude,opencode,fleet,registry}.py](../backend/app/orchestrator/)
- [backend/app/{db,models,schemas,config,litellm_client,main}.py](../backend/app/)
- [backend/app/routes/{budget,fleet,models}.py](../backend/app/routes/)

---

## Severity legend

| Tag                | Meaning                                                                  |
| :----------------- | :----------------------------------------------------------------------- |
| 🔴 **Critical**    | Data loss, runtime crash, resource leak, or security exposure under realistic use. |
| 🟠 **High**        | Broken under modest concurrency or specific failure modes; correctness gap.       |
| 🟡 **Medium**      | Performance / robustness penalty; user-visible degradation but not a crash.      |
| 🔵 **Low**         | Hygiene, maintainability, or future-proofing.                                     |

---

## 🔴 1. `FleetProvider._t0` is shared singleton state — race under concurrency  ✅ FIXED

**Location:** [backend/app/orchestrator/fleet.py:334-344](../backend/app/orchestrator/fleet.py#L334-L344)

```python
class FleetProvider:
    def __init__(self) -> None:
        self._t0 = 0.0

    async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
        self._t0 = time.time()                                # ← writes singleton state
        ...
        yield Event(type="assistant.done",
                    data={"duration_ms": int((time.time() - self._t0) * 1000)})
```

**Root cause.** `FleetProvider` is a process-wide singleton (registered once via the orchestrator registry). `self._t0` is mutable instance state. If two fleet turns run concurrently — two browser tabs, two sessions, the user opening a second chat while one is still streaming — the second `run()` overwrites `self._t0` and the first turn's `assistant.done` reports a wildly wrong (negative or near-zero) `duration_ms`.

**Fix.** Make the start time a local variable. The instance has no real reason to hold it.

```python
async def run(self, ctx: RunContext) -> AsyncIterator[Event]:
    t0 = time.time()
    cfg = load_fleet_config(ctx.cwd)
    ...
    yield Event(type="assistant.done",
                data={"duration_ms": int((time.time() - t0) * 1000)})
```

Drop `self._t0` from `__init__`. (Same audit should be repeated on every `Provider` for any other instance state.)

---

## 🔴 2. WebSocket disconnect mid-stream leaks the provider generator  ✅ FIXED

**Location:** [backend/app/routes/sessions.py:174-205](../backend/app/routes/sessions.py#L174-L205)

```python
async for ev in _safe_run(provider, ctx):
    ...
    await websocket.send_json(ev.to_json())   # raises if client is gone

if text_buf:
    assistant_blocks.append(...)
async with session_scope() as db:
    db.add(Message(...assistant turn...))
```

**Root cause.** Two distinct issues, both triggered by client disconnect mid-turn:

1. `websocket.send_json` raises (`WebSocketDisconnect` / `RuntimeError`). The exception is **not** caught inside the `async for` loop, so it propagates out of `_safe_run`, which means the inner `provider.run(ctx)` async generator is never `aclose()`'d cleanly. For the OpenCode provider this leaves an open SSE stream + httpx connection. For Claude, the spawned `claude` CLI subprocess is left to be reaped by the GC — usually quickly, but not guaranteed.
2. The post-loop block that **persists the assistant message** never runs. Result: the user message is in the DB but no assistant reply, and the chat history reload looks truncated.

**Fix.** Wrap the iteration in `try/finally`, persist whatever has been accumulated so far, and explicitly close the generator on early exit:

```python
events = _safe_run(provider, ctx)
try:
    async for ev in events:
        # ... accumulate into assistant_blocks ...
        try:
            await websocket.send_json(ev.to_json())
        except (WebSocketDisconnect, RuntimeError):
            # Client gone. Stop streaming but still persist what we have.
            break
finally:
    await events.aclose()      # ensures provider.run's finally runs
    if text_buf:
        assistant_blocks.append({"type": "text", "text": "".join(text_buf)})
    if assistant_blocks:
        async with session_scope() as db:
            db.add(Message(
                session_id=session_id, role="assistant",
                content=assistant_blocks,
                cost_usd=cost_usd, duration_ms=duration_ms,
            ))
```

Also wrap the WS handler's outer `try/finally` so `WebSocketDisconnect` from `receive_text()` doesn't bypass cleanup. The current code does the right thing there (`return` inside `try:` reaches the `finally`).

---

## 🔴 3. Two concurrent turns on the same session corrupt each other  ✅ FIXED

**Location:** [backend/app/routes/sessions.py:97-211](../backend/app/routes/sessions.py#L97-L211)

**Root cause.** A user with the chat open in two tabs, or who sends a second prompt before the first completes, hits this:

- Both WS handlers share the same `session_id`, the same `provider` singleton, and (for OpenCode) the same `upstream_id`.
- Both call `provider.run(ctx)`. For OpenCode, both subscribe to the global `/event` firehose. The `_TurnState.user_msg_ids` filter is per-call, but both calls share the *same* upstream session id, so the user-msg of turn A is observable in turn B's stream — and vice versa. Output gets duplicated, interleaved, or lost depending on timing.
- The DB writes from the two turns interleave in unpredictable order, scrambling the assistant message ordering relative to the user prompts.

**Fix.** Per-session asyncio lock around the turn (create lock on first WS connection, store in a module-level `dict[str, asyncio.Lock]` or use a weak dict). A second prompt while the first is in-flight should either queue or be rejected.

```python
# routes/sessions.py
_session_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()

async def _session_lock(session_id: str) -> asyncio.Lock:
    async with _locks_guard:
        return _session_locks.setdefault(session_id, asyncio.Lock())

# In chat_ws, around the per-turn body:
lock = await _session_lock(session_id)
async with lock:
    # ... send session.started, run provider, persist messages ...
```

Long-term: also clean up `_session_locks` when sessions are deleted.

---

## 🔴 4. Provider singletons are not async-safe at first-call  ✅ FIXED

**Location:** [backend/app/orchestrator/registry.py:11-24](../backend/app/orchestrator/registry.py#L11-L24)

```python
_singletons: dict[str, Provider] = {}

def get_provider(name) -> Provider:
    if name not in _singletons:
        if name == "claude":   _singletons[name] = ClaudeProvider()
        elif name == "opencode": _singletons[name] = OpenCodeProvider()
        elif name == "fleet":    _singletons[name] = FleetProvider()
    return _singletons[name]
```

**Root cause.** Two coroutines hitting `get_provider("opencode")` simultaneously when the dict is empty can both pass the `if name not in _singletons` check, both construct an `OpenCodeProvider`, and the second write wins — the first instance is leaked (its `httpx.AsyncClient` is never closed). On uvicorn startup with two near-simultaneous requests this is a real race.

**Fix.** Either eager-initialize on app startup (in `main.py`'s `lifespan`), or use a lock:

```python
_singletons: dict[str, Provider] = {}
_lock = asyncio.Lock()

async def get_provider(name) -> Provider:
    if name in _singletons:
        return _singletons[name]
    async with _lock:
        if name in _singletons:                   # double-checked
            return _singletons[name]
        _singletons[name] = _build_provider(name)
        return _singletons[name]
```

Eager init is simpler — providers are cheap to construct, and you can validate config at startup:

```python
# main.py lifespan
async def lifespan(app):
    for name in ("claude", "opencode", "fleet"):
        get_provider(name)   # warm the cache
    ...
```

---

## 🟠 5. Sync filesystem I/O in async path: `load_fleet_config` runs every fleet turn  ✅ FIXED

**Location:** [backend/app/orchestrator/fleet.py:142-184](../backend/app/orchestrator/fleet.py#L142-L184)

`Path.resolve()`, `Path.is_file()`, and `path.read_text()` are blocking syscalls. They're called from inside `FleetProvider.run()` — i.e., on the event-loop thread, on every fleet turn (and once more per turn through `/api/fleet/config` polling from the UI). On a fast SSD it's microseconds; on NFS, a network FS, or a watched directory under heavy IO, this can be 10–100ms per call and stalls every other request.

**Fix.** Two options, pick whichever fits:

**Option A — cache by mtime, invalidate on change** (zero extra threads, simple):

```python
_cfg_cache: tuple[float, FleetConfig] | None = None

def load_fleet_config(cwd=None) -> FleetConfig:
    global _cfg_cache
    path = _resolve_config_path(cwd)
    if path is None:
        return DEFAULT_FLEET_CONFIG
    mtime = path.stat().st_mtime
    if _cfg_cache and _cfg_cache[0] == mtime:
        return _cfg_cache[1]
    cfg = _parse_and_merge(path)
    _cfg_cache = (mtime, cfg)
    return cfg
```

**Option B — push the I/O off the loop** (fine for one-off file reads):

```python
async def load_fleet_config_async(cwd=None) -> FleetConfig:
    return await asyncio.to_thread(load_fleet_config, cwd)
```

Caller becomes `cfg = await load_fleet_config_async(ctx.cwd)`. Keep both cwd-based and env-based resolution in the cache key if you do option A.

---

## 🟠 6. `delete_all_sessions` does N round-trips  ✅ FIXED

**Location:** [backend/app/routes/sessions.py:67-73](../backend/app/routes/sessions.py#L67-L73)

```python
rows = (await db.execute(select(Session))).scalars().all()
for s in rows:
    await db.delete(s)
await db.commit()
```

**Root cause.** ORM `db.delete(obj)` issues one DELETE per row + cascades. With 100 sessions × (1 sessions + N messages each) cascade, you're waiting on hundreds of round-trips. Postgres locks held the whole time.

**Fix.** Use bulk DML — Postgres `ON DELETE CASCADE` (already declared on `messages.session_id`) handles the children:

```python
from sqlalchemy import delete

@router.delete("", status_code=204)
async def delete_all_sessions(db: AsyncSession = Depends(get_session)) -> None:
    await db.execute(delete(Session))
    await db.commit()
```

Drop the `cascade="all, delete-orphan"` ORM relationship cascade or keep it — the FK `ondelete="CASCADE"` is what matters at the DB level. (One catch: a bulk DELETE bypasses ORM-level cascades, but that's fine because the FK enforces the cleanup at the database.)

---

## 🟠 7. `get_session` dependency leaks transactions on raise  ✅ FIXED

**Location:** [backend/app/db.py:32-34](../backend/app/db.py#L32-L34)

```python
async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
```

**Root cause.** No commit, no rollback, no explicit transaction lifecycle. Routes call `await db.commit()` themselves. If a route raises *before* its `commit()`, the `async with SessionLocal()` exits — `__aexit__` calls `close()`, which releases the connection but does NOT commit the in-flight transaction. With autoflush + autobegin, you can leave dirty state in the connection that's then returned to the pool. SQLAlchemy 2.x usually issues an implicit ROLLBACK on close, so it's not a corruption bug — but the contract is fragile and the routes are inconsistent (`session_scope` does the right thing, `get_session` doesn't).

**Fix.** Make `get_session` mirror `session_scope` — auto-commit on successful exit, rollback on exception. Routes drop their explicit `await db.commit()`.

```python
async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

Then in routes, remove `await db.commit()` lines (they become no-ops or trigger a "nothing to commit" warning).

---

## 🟠 8. `GET /api/sessions/:id/messages` is unbounded  ✅ FIXED

**Location:** [backend/app/routes/sessions.py:76-85](../backend/app/routes/sessions.py#L76-L85)

```python
rows = (await db.execute(
    select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
)).scalars().all()
```

**Root cause.** No `LIMIT`. A long chat (100 turns, multi-megabyte JSON `content`) returns the entire thing every time the user opens the session — and we re-fetch on each `session?.id` change in the frontend. This is unbounded growth in both response size and DB read load.

**Fix.** Add cursor pagination:

```python
@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def get_messages(
    session_id: str,
    before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_session),
) -> list[MessageOut]:
    stmt = select(Message).where(Message.session_id == session_id)
    if before:
        stmt = stmt.where(Message.created_at < before)
    stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [MessageOut.model_validate(r, from_attributes=True) for r in reversed(rows)]
```

Frontend hydrates with the latest 50 and lazy-loads on scroll-up.

---

## 🟠 9. WebSocket has no idle timeout / heartbeat  ✅ FIXED

**Location:** [backend/app/routes/sessions.py:122-126](../backend/app/routes/sessions.py#L122-L126)

```python
while True:
    try:
        raw = await websocket.receive_text()    # blocks forever
    except WebSocketDisconnect:
        return
```

**Root cause.** A client that goes silent (laptop closed, network split) holds an open WS forever. uvicorn's default keep-alive doesn't time these out. Each idle connection holds a Postgres connection-pool slot indirectly (because each turn re-acquires) and consumes a TCP socket and a coroutine.

**Fix.** Use `asyncio.wait_for` with a generous timeout, plus periodic ping frames:

```python
import asyncio

WS_IDLE_TIMEOUT = 30 * 60   # seconds
WS_PING_INTERVAL = 30

async def _keepalive(ws: WebSocket) -> None:
    while True:
        await asyncio.sleep(WS_PING_INTERVAL)
        try:
            await ws.send_json({"type": "ping", "data": {}})
        except Exception:
            return

ping_task = asyncio.create_task(_keepalive(websocket))
try:
    while True:
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=WS_IDLE_TIMEOUT
            )
        except asyncio.TimeoutError:
            await websocket.close(code=1001, reason="idle timeout")
            return
        # ...
finally:
    ping_task.cancel()
```

Frontend already has reconnect logic, so closing idle sockets is friendly.

---

## 🟠 10. `LOCALCODE_FLEET_CONFIG` honored from env on every turn — env mutability surprise  ✅ FIXED

**Location:** [backend/app/orchestrator/fleet.py:156-158](../backend/app/orchestrator/fleet.py#L156-L158)

`os.environ.get("LOCALCODE_FLEET_CONFIG")` is read fresh every turn. That sounds like a feature ("hot reload!") but in practice users edit `.env` and don't realize the Pydantic settings loader has already cached the rest of the config (`get_settings()` is `lru_cache`'d). Inconsistent behavior — fleet path reloads but `litellm_api_base` doesn't.

**Fix.** Pull `LOCALCODE_FLEET_CONFIG` through `Settings` so it reloads (or doesn't) consistently with the rest of the env. If hot-reload is desirable, drop the `lru_cache` on `get_settings()` and accept the per-call cost (it's tiny).

---

## 🟡 11. Catalog model partition assumes `openai` for unprefixed names  ✅ FIXED

**Location:** [backend/app/orchestrator/opencode.py:51-56](../backend/app/orchestrator/opencode.py#L51-L56)

```python
provider_id, _, model_id = ctx.model.partition("/")
if not model_id:
    provider_id, model_id = "openai", provider_id
```

**Root cause.** A user picking a model id without a `/` (typo, or a custom model name) gets silently routed to `openai/<model>`. If they meant `anthropic/<model>` they get an opaque "model not found" from OpenCode much later in the request.

**Fix.** Fail fast with a clear error event:

```python
provider_id, _, model_id = ctx.model.partition("/")
if not model_id:
    yield Event(type="error", data={
        "message": f"opencode model {ctx.model!r} must be in 'provider/model' form (e.g. openai/gpt-5.4)",
        "provider": self.name,
    })
    return
```

---

## 🟡 12. Fleet steps run sequentially even when independent  ⏸ DEFERRED

**Why deferred.** Implementing this correctly requires the planner to declare *all* cross-step dependencies — including filesystem state, not just data flow. Two coder steps editing the same file in parallel race silently. The current sequential model is correct-by-construction; parallelism is an optimization that needs a proper workflow engine to be safe. Reopen when (a) we add explicit per-step tool-permission scoping, or (b) we ship a DAG-aware runner.

**Location:** [backend/app/orchestrator/fleet.py:397-399](../backend/app/orchestrator/fleet.py#L397-L399)

```python
for step in plan.steps:
    async for ev in self._run_step(step, ctx, cfg, outputs):
        yield ev
```

**Root cause.** `depends_on` is parsed from the planner's output but execution is unconditionally serial. Two independent steps wait for each other. For long plans (4–6 steps), this is the dominant latency.

**Fix.** Topological-sort steps into "waves" that can run concurrently:

```python
async def _run_plan(self, plan, ctx, cfg):
    waves = topo_waves(plan.steps)              # list[list[Step]]
    outputs: dict[str, str] = {}
    for wave in waves:
        # Run all steps in this wave in parallel; serialise events.
        results = await asyncio.gather(
            *[_run_step_collect(step, ctx, cfg, outputs) for step in wave]
        )
        for step, events, output in results:
            for ev in events: yield ev
            outputs[step.id] = output
```

Reviewer steps almost always depend on a coder/developer step, so they don't parallelize — but two coder steps with no `depends_on` between them do.

(Bigger change. Reasonable to defer until v2.)

---

## 🟡 13. `httpx.AsyncClient` has no connection-pool size cap  ✅ FIXED

**Location:** [backend/app/orchestrator/opencode.py:26-29](../backend/app/orchestrator/opencode.py#L26-L29) & [backend/app/litellm_client.py:16](../backend/app/litellm_client.py#L16)

`httpx.AsyncClient()` with no `limits=` defaults to 100 keepalive + 100 max — usually fine, but a runaway loop (e.g., reconnect storm against a flapping OpenCode) can exhaust file descriptors.

**Fix.** Set explicit limits:

```python
self._client = httpx.AsyncClient(
    base_url=...,
    timeout=httpx.Timeout(60.0, read=None),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)
```

---

## 🟡 14. `raw` column on `messages` is dead weight  ✅ FIXED

**Location:** [backend/app/models.py:56](../backend/app/models.py#L56)

The `raw: dict | None` column is declared but never written. Either start writing the raw provider message there (useful for debugging) or drop the column.

**Fix.** Pick one — either write `raw=ev.data` from the WS handler, or remove the column. (Until then, the cost is one always-NULL JSONB column per row.)

---

## 🟡 15. `_translate` (Claude) drops final-message text dedup if SDK behavior changes  ⏸ DEFERRED

Optional / forward-looking; flagged for the next SDK upgrade rather than today.

**Location:** [backend/app/orchestrator/claude.py:98-102](../backend/app/orchestrator/claude.py#L98-L102)

The dedup logic — *"emit text deltas from `StreamEvent`, skip `TextBlock`s on the final `AssistantMessage`"* — is only correct if `include_partial_messages=True` AND the SDK actually emits matching deltas before the final block. If a future SDK update streams text via `TextBlock` directly without `StreamEvent`s, this logic silently drops the text.

**Fix.** Defensive verification — count delta chars vs. final block text length and emit a warning (or the missing tail) if they diverge:

```python
# Track total delta chars per content_block index.
# At final-message time, if a TextBlock is longer than what we streamed,
# emit only the unstreamed tail. Cheap insurance against SDK churn.
```

(Optional — only worth it if SDK upgrades cause visible bugs.)

---

## 🟡 16. `cost_usd` is `Float` (binary), not `Numeric`  ✅ FIXED

**Location:** [backend/app/models.py:54](../backend/app/models.py#L54)

```python
cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
```

Binary floats accumulate rounding error. Sum 10,000 turns and you'll see drift. Local-only, low stakes today, but every "cost" or "money" field should be `Numeric(12, 6)` from day one.

**Fix.** `mapped_column(Numeric(12, 6), nullable=True)` and decode to `Decimal` on read.

---

## 🟡 17. CORS allow-list is hardcoded  ✅ FIXED

**Location:** [backend/app/main.py:27-33](../backend/app/main.py#L27-L33)

```python
allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"]
```

Fine for local dev, but moves to staging/prod require code edits. Should be settings-driven.

**Fix.** Add `cors_origins: list[str]` to `Settings`, default to the localhost values; read here.

---

## 🟡 18. Path-traversal via session `cwd`  ✅ FIXED

**Location:** [backend/app/routes/sessions.py:54-60](../backend/app/routes/sessions.py#L54-L60), [backend/app/orchestrator/claude.py:58](../backend/app/orchestrator/claude.py#L58)

`CreateSessionRequest.cwd` is user-controlled and passed directly to `ClaudeAgentOptions(cwd=...)`. The spawned `claude` CLI then runs in that directory — including its file-edit tools. A malicious or careless frontend can set `cwd="/etc"` and have Claude edit files there.

**Fix.** Validate against an allowlist: pre-configured project root(s), or at minimum `cwd` must be a subdirectory of the orchestrator's CWD. Reject anything else with HTTP 400.

```python
ALLOWED_ROOTS = [Path("/Users/you/Projects").resolve()]   # from settings

def _validate_cwd(cwd: str | None) -> str | None:
    if cwd is None:
        return None
    p = Path(cwd).expanduser().resolve()
    if not any(p == r or r in p.parents for r in ALLOWED_ROOTS):
        raise HTTPException(400, f"cwd not in allowed roots: {p}")
    return str(p)
```

---

## 🔵 19. `_safe_run` final `assistant.done` may be sent after WS is closed  ✅ FIXED (subsumed by #2)

**Location:** [backend/app/routes/sessions.py:36-38](../backend/app/routes/sessions.py#L36-L38)

If the provider's last yielded event is an `error` (e.g., from inside `provider.run`'s own try/except), `_safe_run` then yields a synthetic `assistant.done`. The outer handler does `send_json` on it — same disconnect-mid-send risk as #2. Once #2 is fixed, this becomes harmless.

---

## 🔵 20. `_session_locks` has no eviction (would leak when sessions are deleted)  ✅ FIXED

If you implement #3, remember to delete the per-session lock when the session is deleted. Otherwise the dict grows forever in long-lived deployments.

---

## 🔵 21. `Text` import in `models.py` is unused  ✅ FIXED

[backend/app/models.py:6](../backend/app/models.py#L6) — `Text` is imported but never used. Lint-level, but worth a sweep with `ruff check --select F401`.

---

## Summary table

| # | Status      | Area               | Severity     | Verification                                                              |
| :- | :---------- | :---------------- | :----------- | :------------------------------------------------------------------------ |
| 1 | ✅          | Concurrency        | 🔴 Critical | Local `t0` in `FleetProvider.run`. Concurrent fleet turns no longer share state. |
| 2 | ✅          | Resource leak      | 🔴 Critical | `events.aclose()` in `finally`; partial assistant text persists on disconnect. Smoke: 163 chars saved after mid-stream WS close. |
| 3 | ✅          | Concurrency        | 🔴 Critical | Per-session `asyncio.Lock`. Two-tab smoke: ALPHA + BRAVO each got correct distinct response, ~22s total (= 2× single-turn). |
| 4 | ✅          | Concurrency        | 🔴 Critical | Async-locked `get_provider` + `warm_up()` on lifespan startup.            |
| 5 | ✅          | Performance        | 🟠 High     | Mtime-keyed cache; one `stat()` per turn instead of YAML parse.            |
| 6 | ✅          | Performance / DB   | 🟠 High     | `db.execute(delete(Session))`. Smoke: bulk delete clears N sessions in 1 round-trip. |
| 7 | ✅          | DB hygiene         | 🟠 High     | `get_session` commits/rolls back like `session_scope`. Routes drop manual `commit()`s. |
| 8 | ✅          | Perf / API         | 🟠 High     | `MessagesPage` with `before` cursor, default 50, max 500. Frontend unwraps `.messages`. |
| 9 | ✅          | Resource leak      | 🟠 High     | 30-min idle timeout via `wait_for`; 30-s ping heartbeat task per WS.       |
| 10 | ✅         | Config consistency | 🟠 High     | `LOCALCODE_FLEET_CONFIG` now a `Settings` field; uniform with the rest of the env.  |
| 11 | ✅         | UX / robustness    | 🟡 Medium   | Unprefixed OpenCode model emits a clear error event with the fix command. |
| 12 | ⏸ deferred | Performance        | 🟡 Medium   | Needs explicit cross-step file-state declarations from the planner first. |
| 13 | ✅         | Robustness         | 🟡 Medium   | `httpx.Limits(max_connections=20)` on OpenCode + LiteLLM clients.          |
| 14 | ✅         | Schema hygiene     | 🟡 Medium   | `messages.raw` dropped from model + DB.                                    |
| 15 | ⏸ deferred | Maintainability    | 🟡 Medium   | Optional; revisit on next SDK upgrade.                                    |
| 16 | ✅         | Numerics           | 🟡 Medium   | `cost_usd` → `Numeric(12, 6)`; API serializer coerces back to float.       |
| 17 | ✅         | Config             | 🟡 Medium   | `cors_origins` (CSV) on `Settings`; `cors_origin_list` property reads it.  |
| 18 | ✅         | Security           | 🟡 Medium   | `_validate_cwd` checks against `Settings.allowed_cwd_roots`. Empty allowlist = permissive. |
| 19 | ✅         | Robustness         | 🔵 Low      | Subsumed by #2's fix.                                                      |
| 20 | ✅         | Memory             | 🔵 Low      | `_drop_session_lock` on delete; `_session_locks.clear()` on bulk delete. |
| 21 | ✅         | Hygiene            | 🔵 Low      | Unused `Text` import removed.                                              |

## What I verified

- **#1, #3** — two simulated tabs sending in parallel against the same session: distinct prompts, distinct correct answers, total time ≈ 2× single-turn (proves serialization).
- **#2** — abrupt WS close after 3 events: 163 chars of partial assistant text persisted to the DB; provider generator correctly aborted (no leftover Claude subprocess).
- **#11** — sending `opencode:gpt-5.4-mini` (no `openai/` prefix) returns a clear `error` event listing how to fix.
- **#6** — `DELETE /api/sessions` with N sessions completes in one DB round-trip.
- **#8** — `GET /messages` returns `MessagesPage` with `messages`, `next_before`, `has_more`.
- **Provider singletons** — `warm_up()` builds all three at startup; the registry's lock prevents post-startup re-entry races.
- **DB schema** — `cost_usd` is now `numeric`, `raw` is dropped (Postgres metadata confirmed).

## Files touched

**Backend.** [config.py](../backend/app/config.py) (new fields), [db.py](../backend/app/db.py), [models.py](../backend/app/models.py), [schemas.py](../backend/app/schemas.py), [main.py](../backend/app/main.py), [orchestrator/registry.py](../backend/app/orchestrator/registry.py), [orchestrator/fleet.py](../backend/app/orchestrator/fleet.py), [orchestrator/opencode.py](../backend/app/orchestrator/opencode.py), [routes/sessions.py](../backend/app/routes/sessions.py), [litellm_client.py](../backend/app/litellm_client.py).

**Frontend.** [api.ts](../frontend/src/api.ts), [types.ts](../frontend/src/types.ts), [components/ChatPane.tsx](../frontend/src/components/ChatPane.tsx).

**Schema migration.** Live `ALTER TABLE` to drop `messages.raw` and convert `messages.cost_usd` from `double precision` to `numeric(12, 6)`. Empty rows / null values handled cleanly.
