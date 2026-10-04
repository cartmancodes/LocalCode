# Rust-only repository — design

Date: 2026-10-04
Status: approved in conversation; awaiting written-spec review

## Intent

The user works only in the Rust terminal UI (`octet`, `crates/`). It drives
the official `claude` and `codex` CLIs directly and needs no Python, browser or
server. The repository still carries a web UI and two Python harnesses that the
user does not use. The user asked to refactor the repository, remove stale and
redundant code, and keep only what the terminal UI needs.

Success: the repository is the Rust workspace and its documentation; `make
rust-check` passes; the TUI runs end to end against real Claude and Codex; no
live file refers to a deleted path; and the one safety guarantee the Python
code enforced (Octet never reads vendor credentials) is still enforced, now
by a Rust test.

This supersedes the "web UI on the core" refactor
(`refactor/web-ui-on-core`, unmerged): with no web UI there is nothing to move
onto the core. That branch is left untouched for the user to delete.

## Removed

Everything below stays in git history; the last commit containing it is the
merge base of this branch (`master` at the time of the change).

- **Python:** `backend/` (legacy stack, Python core and its `octet` CLI,
  `eval/`, all tests and fixtures), `packages/fleet/`, `pyproject.toml`,
  `.env.example`, `.octet/` (fleet config and examples).
- **Web:** `frontend/`, `vscode-extension/`, `setup.sh`.
- **Makefile targets:** `install`, `up`, `down`, `logs`, `status`, `backend`,
  `frontend`, `dev`, `test`, `lint`, `typecheck`, `soak`, `format`,
  `codex-schema` (it reconciled the Python Codex client's schema).
- **Docs about the removed system:** `docs/architecture.md`, `codex.md`,
  `core.md`, `fleet.md`, `fleet-config.md`, `harness.md`, `harness-roadmap.md`,
  `roadmap.md`, `storage.md`, `vscode-integration.md`,
  `docs/superpowers/specs/2026-09-14-eval-foundation-design.md`,
  `docs/superpowers/plans/2026-09-14-eval-foundation.md`,
  `docs/superpowers/plans/harness-roadmap.md`.

## Kept

`crates/`, `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml`,
`scripts/rust-env.sh`, the Makefile's Rust targets (`help`, `rust-check`,
`rust-build`, `tui`, `tui-demo`), `docs/tui.md`, `docs/rust/`, and the Rust
specs, plans and reviews under `docs/superpowers/` and `docs/reviews/`. Specs,
plans and reviews are dated historical records: they keep their references to
Python paths as written.

## Changed

- `README.md`: rewritten for the terminal UI only (what it is, prerequisites,
  build, run, permission modes and commands in brief, where the docs are).
- `.gitignore`: drop Python and Node entries (`__pycache__/`, `*.pyc`,
  `.venv/`, `.env`, `.mypy_cache/`, `.pytest_cache/`, `ruff_cache/`,
  `.ruff_cache/`, `node_modules/`, `dist/`, `build/`, `coverage/`,
  `*.tsbuildinfo`, `/.run/`, `/.octet/sessions/`); keep `/target/`,
  `.superpowers/`, `.claude/`, `.DS_Store`, `.vscode/`.
- `Makefile`: Rust targets only; `help` lists them.
- `docs/rust/parity-matrix.md`: its reference column names Python test files
  that no longer exist in the tree; a note says they are read at the merge-base
  commit (`git show <sha>:backend/tests/<file>`), with the SHA written out.
- `docs/rust/remote-control-plan.md`, `docs/tui.md`: remove statements that
  the Python CLI/RPC modes "remain available".
- `crates/octet/src/main.rs`: the non-terminal error message stops
  pointing at Python RPC/print modes.
- `crates/octet/tests/terminal.rs`: header comment unchanged in meaning
  ("No Python, browser, or vendor login is needed" stays true); no change
  needed.

## Credential guard (new)

The Python test `backend/tests/test_auth_invariant.py` (with
`backend/app/invariants.py`) failed the build if harness code named a vendor
credential store or assigned a secret-looking environment variable. Deleting
Python would drop that protection silently.

A Rust integration test, `crates/octet/tests/credentials.rs`, scans every
`*.rs` file under `crates/` (excluding `target/` and itself) and fails if:

1. any string literal contains one of: `.credentials.json`,
   `credentials.json`, `/auth.json`, `auth.json`, `.claude/.credentials`,
   `.codex/auth`, `find-generic-password`, `CLAUDE_CODE_OAUTH_TOKEN`,
   `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_SESSION_KEY`,
   `ANTHROPIC_AUTH_TOKEN` (the Python marker list, unchanged);
2. any `.env("…")` call names a variable ending in `API_KEY`, `OAUTH_TOKEN`,
   `SESSION_KEY` or `AUTH_TOKEN`.

It reports file, line and rule for each violation. The current Rust source has
no violations. The test proves it can fail by scanning an in-memory sample with
each kind of violation.

## Verification

- `make rust-check` (format, all workspace tests including the new guard,
  clippy `-D warnings`).
- `git grep` over live files (everything except `docs/superpowers/` and
  `docs/reviews/`) finds no reference to `backend/`, `frontend/`,
  `vscode-extension`, `packages/fleet`, `pyproject`, `setup.sh`, `.venv` or
  the deleted docs.
- The release binary passes the existing PTY scenarios end to end against the
  real `claude` and `codex` CLIs (the scripts used for the 2026-10-04 checks).
- `make help` lists only working targets, and each one runs.

## Out of scope

- Porting fleet, extensions/skills/packages or print/JSON/RPC modes to Rust.
- Deleting the `refactor/web-ui-on-core` branch or the user's running web
  services and local `~/.octet` data.
