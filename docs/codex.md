# Codex (`codex app-server`)

The `codex` provider drives the **official Codex CLI** the same way the
`claude` provider drives the official Claude CLI: LocalCode spawns the vendor's
binary and lets it find its own OAuth credentials. LocalCode never reads
`~/.codex/auth.json`, never sets `OPENAI_API_KEY`, and has no fallback to an
API key — a missing binary is an error message, never a quiet switch to a paid
key. (`backend/app/invariants.py` enforces this; `backend/tests/test_auth_invariant.py`
is the gate.)

ChatGPT-side work used to be routed through a third-party `opencode` provider,
now retired. What the first-party binary buys over that arrangement is why this
provider looks the way it does:

| | `opencode` (retired) | `codex` |
| :-- | :-- | :-- |
| Tool approvals | none — the server decided alone | the **same** approval card as Claude, from the same `evaluate_tool_request` |
| Extra directories | not supported (one project dir per session) | `additional_dirs` is forwarded to `thread/start` |
| Transport | third-party HTTP + SSE, filtered client-side | first-party JSON-RPC over the child's stdio |
| Role policies | advisory | the shared `ToolPolicy` table decides every approval the app-server sends — but see [What the role policy does not cover](#what-the-role-policy-does-not-cover) |

## Prerequisites

1. Install the Codex CLI (`npm i -g @openai/codex`, or Homebrew — follow
   OpenAI's current instructions).
2. Authenticate **once, in the CLI**:

   ```bash
   codex login
   ```

   This writes the CLI's own credential store. LocalCode does not read it, and
   does not want to.
3. Check the binary is on `PATH`:

   ```bash
   codex --version
   ```

If the binary is missing, a Codex turn produces exactly one `error` event
naming the binary and telling you to run `codex login`. Nothing is retried
against an API key.

### Settings

| Setting (env var) | Default | What it is |
| :-- | :-- | :-- |
| `codex_binary` (`CODEX_BINARY`) | `codex` | A **name**, not a credential. Point it at an absolute path for a non-`PATH` install. |
| `codex_startup_timeout_s` (`CODEX_STARTUP_TIMEOUT_S`) | `30.0` | Spawn plus the `initialize` handshake. |
| `codex_request_timeout_s` (`CODEX_REQUEST_TIMEOUT_S`) | `120.0` | Per-request ceiling once the server is up. |

One `codex app-server` process is held **per workspace**, across turns, behind
`CodexBroker`. Cold start is paid once per directory, not once per message, and
the whole process *group* is killed on shutdown (the app-server spawns helpers
of its own; signalling only the leader leaves them running).

## Using it

Pick `codex` as the provider when creating a session, exactly like `claude`.
Approvals, extra directories, permission modes and role policies all behave
identically — that is the point of the provider.

### What the role policy does not cover

**LocalCode does not set the app-server's approval policy.** `thread/start`
sends `{cwd, model, additionalDirectories}` and nothing else, so *whether*
`execCommandApproval` / `applyPatchApproval` are sent at all is decided by the
user's own `~/.codex` configuration (its approval and sandbox settings). Every
request that does arrive is answered by `evaluate_tool_request` under the
session's `ToolPolicy` — the same table, the same card, the same deny a Claude
tool gets — but a server configured to ask about nothing gives LocalCode
nothing to refuse. A `codex` role is therefore bounded by the shared table
*and* by that configuration, not by the table alone.

Sending an explicit policy on `thread/start` is the fix, and it is a follow-up
gated on reconciling the real schema (`make codex-schema`): guessing a field
name here would produce a policy the server silently ignores, which reads
exactly like one it enforces. Recorded as A12 in
`backend/app/orchestrator/codex/protocol.py`.

### The one-line switch for the fleet's coder

The fleet's `coder` role ships as `claude:claude-sonnet-4-6` **on purpose**:
the `codex` binary is not installed on every machine, and a default that cannot
run is worse than one that can. The alternative is already written out,
commented, in `backend/app/orchestrator/fleet/defaults.py`:

```python
    "coder": RoleConfig(
        provider="codex",
        model="gpt-5.3-codex",
        system_prompt=CODER_SYSTEM,
    ),
```

Swap that in for the `claude` entry beside it and the coder runs on Codex —
a cheaper model doing the mechanical work, with real approvals and real
`additional_dirs`.

**No code change is needed to try it.** The fleet editor's provider dropdown is
rendered from the backend's `valid_providers`, so `codex` is selectable per
role in the UI, and a role's model list comes from the catalog entry for
whichever provider the role is on. The same switch can also be made in a fleet
config file — see [fleet-config.md](fleet-config.md). Editing `defaults.py` is
only for changing what every *new* workflow starts with.

## When the protocol changes

The app-server is **explicitly experimental**: method names, item kinds and
field spellings move between CLI releases. The code is arranged so that costs
one file.

* `backend/app/orchestrator/codex/protocol.py` holds **every wire name** — each
  method, notification, item type, decision string, error code, the
  `app-server` subcommand, **and every field name**: the ones we write as `F_*`
  constants, the ones we read as `*_FIELDS` tuples splatted into `pick()`, and
  the field *values* that carry meaning (`CREATE_KINDS`, `FAILED_STATUSES`).
  It is the only module a schema change should need to touch; no other module
  in the package spells a wire name, and each read field can carry several
  historical spellings at once, so a `snake_case` ↔ `camelCase` flip costs
  nothing at all.

  The two exceptions, both deliberate and neither a wire name: the turn
  stream's internal `{"method", "params"}` envelope, which is a contract
  between `client.py` and `provider.py` (documented where it is defined), and
  the JSON-RPC 2.0 envelope itself (`jsonrpc`, `id`, `result`, `error`), which
  belongs to the transport and not to Codex.

* That module's docstring also lists **ten numbered assumptions, every one
  marked UNVERIFIED** against the real binary, with the cost of each being
  wrong. Read them before trusting anything here. `A3` is the one to check
  first: the code assumes `turn/start` is answered as a *prompt
  acknowledgement*, not at turn end. If the real server answers only on
  completion, the awaited request blocks the loop that drains the item queue
  and every turn longer than `codex_request_timeout_s` fails with a timeout —
  the one assumption whose failure costs a turn rather than an unrendered
  block.
* `backend/tests/fakes/fake_codex_app_server.py` **duplicates** those names
  deliberately (it runs as a child process and imports nothing from the app).
  Update it alongside `protocol.py` — if you do not, the tests fail, which is
  exactly the signal you want.
* No translator branch may raise on a name it does not know. An unknown item
  type is logged once at `DEBUG` and skipped: a newer CLI introducing an item
  kind must cost one unrendered block, never a failed turn.

To reconcile against the real thing:

```bash
make codex-schema     # codex app-server generate-json-schema > docs/codex-app-server.schema.json
```

Then diff the generated schema against `protocol.py` and fix any drift there.
The `requires_cli`-marked test in `backend/tests/test_codex_provider.py` is the
live check that the handshake still works:

```bash
.venv/bin/pytest backend/tests/test_codex_provider.py -m requires_cli -o addopts=""
```

It is excluded from the default suite (see `addopts` in `pyproject.toml`) and
skips when `codex` is not on `PATH`. Everything else is verified against the
fake, which needs no binary, no network and no subscription.

## See also

- [harness.md](harness.md) — the contract every provider is held to, and where
  Codex sits in the provider table, the one approval bus and the quota ledger.
  `backend/tests/test_matrix.py` runs the same cells against `claude` and
  `codex`, single and in a fleet; `backend/tests/replay/codex_*.json` pin the
  translation to fixtures written from the spellings in `protocol.py`. Both
  agree with this module's reading of the protocol **by construction**, so
  they cannot falsify a wrong guess about the vendor's schema — only
  `make codex-schema` and a reconciliation can.
- [storage.md](storage.md) — where a Codex turn's transcript lands on disk.
