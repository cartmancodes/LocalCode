# Review fixes: design

Date: 2026-10-07. Source: the senior review of `master` at 2b32d4e (14 findings).
Status: proposed.

## Intent

Fix every finding from the review. Octet's behaviour stays the same except in
two places: `@` ranking gets faster, and stale wording is removed. The 252
existing tests are the safety net. Each finding ends with a test, a lint or a
CI step that would catch it coming back.

**Out of scope:** new features, the real Claude and Codex CLIs, and the phone
path.

## Decisions

### 1. CI (finding 1)

Add `.github/workflows/check.yml`:

- **Triggers:** pushes to `master` and pull requests.
- **Runner:** macos-latest, the platform Octet is used on. Add ubuntu-latest
  too, since `octet-proc` targets Linux as well.
- **Toolchain:** installed from `rust-toolchain.toml` (1.98.1).
- **Steps:** `make rust-check`, then `cargo audit` through `rustsec/audit-check`.
- **Speed:** the cargo registry and `target/` are cached.

Duplicate crate versions (`thiserror` 1/2, `syn` 2/3, `hashbrown`) all come in
through crossterm. Octet can't remove them, so they aren't checked.

### 2. The protocol gate gets its own crate (finding 2)

**What moves into a new `octet-gate` crate:**

- `GateProcess`, `GateError` and the fixture reply functions, now in the
  crate's `lib.rs`;
- the `protocol-gate` binary;
- the `contracts.rs` and `protocol_gate.rs` tests.

`octet-engine` then holds only product code.

**Sharing the reply code.** The gate stops keeping its own copies of the
default replies (`codex_response_for_request`, `claude_response_for_request`).
It calls the production `codex_stray_reply` and `claude_stray_reply`, which
`octet_engine::live` makes public. The duplicate-wording test in `driver.rs`
then has nothing to compare, so it is reduced to its production assertions.

The gate's replies change from "unsupported request in protocol fixture" to
Octet's own wording, which has the same meaning. The gate's tests check
decisions, not wording.

### 3. One command registry (finding 3)

**The registry.** A new module, `octet-tui/src/commands.rs`, holds
`pub const COMMANDS: &[Spec]`. Each entry has:

- `name`;
- `aliases` (`/exit` for `/quit`);
- `usage`, the help-screen line;
- `summary`, the palette line;
- `quick`, an optional short label for the sidebar;
- `id`, a `Cmd` enum value.

**Dispatch.** `try_command` parses the input to a `Cmd` and matches on it
exhaustively, so a registry entry without a handler doesn't compile.

**What's built from the registry:**

- the palette;
- Tab completion;
- the help screen's command lines, from `usage` (the key lines stay a fixed
  list);
- the sidebar's "QUICK COMMANDS";
- the CLI `--help` "Commands:" line (`octet` already depends on `octet-tui`).

**Tests:**

- every entry parses to its `Cmd`;
- every `Cmd` has exactly one entry;
- help, the palette and the CLI help each list every entry.

### 4. Faster `@` ranking (finding 4)

**The fix.** `Index::rank` reuses three scratch buffers across all paths, and
swaps the rolling rows instead of allocating a new one per character. Before
running the matching, it skips any path that lacks one of the query's
characters. Scoring is unchanged.

**Tests:**

- a property test checks that the new ranking equals the old one on generated
  paths and queries;
- an ignored release-mode test (`cargo test --release -- --ignored`) checks
  that ranking 50,000 paths stays under 25 ms. Today it takes 71–119 ms.

### 5. Small duplicates in `lib.rs` (finding 5)

| Duplicate | Replacement |
| --- | --- |
| History push, twice | `App::remember(draft)` |
| "Prompt limit reached", six times | `App::insert_or_warn(text)` and `replace_or_warn` |
| Three identical newline arms | One arm |
| Session-resume logic, twice | `resume_from(&app, &config)` |
| Editor resume, resize, new reader; three times | `regain_terminal(guard, terminal, &mut input)` |

### 6. Smaller files (finding 6)

`octet-tui/src/lib.rs` (1,965 lines) splits into:

- `lib.rs`: `run` and the session loop;
- `input.rs`: `key_action`, paste, the palette, completion and the Tab
  handling. `key_action` is broken into per-mode handlers: help, approval,
  palette, completion and composer.
- `commands.rs`: the registry and `try_command`, one function per command
  where a handler is longer than about 10 lines.

`view.rs` (1,719 lines) splits into:

- `app.rs`: the `App` state, its event handling and the transcript cache;
- `view.rs`: drawing only. `draw` is split into header, conversation,
  composer and overlays.

Tests move with the code they cover. No file should end above about 800 lines
including its tests.

### 7. More reliable tests (finding 7)

- **Fixed sleeps.** Sleeps that wait for something to happen become
  condition-based waits, through a shared `wait_until(timeout, condition)` in
  `octet-testkit`. Sleeps that are part of the scenario stay: for example,
  "hold for 200 ms, then cancel".
- **Timer logic.** Tests of timer logic that runs no real processes use
  `tokio::time::pause()`.
- **The `perl -MPOSIX` dependency.** It is replaced by a small `octet-testkit`
  binary, `detached-sleep`, that calls `setsid` and sleeps.
- **The unexplained ignore.** `transport.rs:266` gets a reason.
- **Wall-clock assertions** stay where a time bound is the behaviour under
  test. They get one shared margin constant instead of numbers picked case by
  case.

### 8. Shell time-limit wording (finding 8)

`Status::TimedOut` carries the limit it hit, and `summary()` formats it ("timed
out after 10 minutes", or "after 300 ms" in the test). The wording then can't
disagree with the constant.

### 9. Stale wording (finding 9)

- **CLI help:** remove the "Fleet, full plugin/hook parity, v3 browsing and
  legacy RPC compatibility remain pending" line.
- **Sidebar:** remove "Preview · core migration remains in progress".
- **`docs/rust/remote-control-plan.md`:** mark its status as superseded. The
  web client it proposes was removed on 2026-10-04, and phone access is now
  `docs/remote-control.md`.

### 10. Hand-packed code (finding 10)

**What changes:**

- the long one-line `json!` and `format!` calls in `octet-core/src/lib.rs` and
  `view.rs` become named locals, which rustfmt can format;
- the help and CLI help strings are built from `&[&str]` lines. This follows
  from decision 3.

**Check:** a test scans the workspace's `.rs` files for lines over 120
characters outside string literals.

### 11. Typed errors at the core boundary (finding 11)

**What changes.** `octet-core`'s public API returns `thiserror` enums instead
of `String`:

- `GoalError`: invalid objective, file, read, save or clear;
- `SelectionError`;
- `SessionError`: journal create or write;
- `ExportError`: read, create or write.

Each error's `Display` text equals today's message byte for byte, and the
existing tests compare that text.

**What stays a string:**

- the TUI's internal leaf functions (`shell::run`, `external::prepare` and
  `finish`). Their only caller shows the text, so a type would add nothing.
- the driver's internal `Result<(), String>`, which is already turned into
  `Event::Error` at the boundary.

### 12. `App` grouped by concern (finding 12)

`App`'s 45 fields become five groups:

| Group | Fields |
| --- | --- |
| `Connection` | engine, mode, model labels, session, journal, ready, running, stopped, status, usage |
| `Transcript` | entries, sizes, sanitizers, scroll, catalog focus, the copyable reply |
| `Composer` | editor, history, attachments, completion, files, a running `!` command |
| `Overlays` | help, palette and selection, approvals and their scroll |
| `App` itself | `notice`, `quit_armed` and `goals`, which every group uses |

Field access changes, for example `app.running` becomes `app.conn.running`.
Logic stays the same. The groups' fields stay `pub(crate)`, and nothing
outside the crate reaches in.

### 13. Build settings (finding 13)

- **tokio features:** replace `"full"` with the features actually used:
  `rt-multi-thread`, `macros`, `process`, `signal`, `sync`, `time`, `io-util`,
  `fs` and `net`. The compiler confirms that list.
- **Release profile:** `lto = "thin"`, `codegen-units = 1`, `strip = true`.
  The binary size before and after is recorded in the commit.

### Selected pedantic lints

Turned on as workspace lints through `[workspace.lints.clippy]`, with every
crate declaring `[lints] workspace = true`:

- `cast_possible_truncation`: its 7 casts become checked or saturating;
- `needless_pass_by_ref_mut`;
- `unused_async`.

`use_self` and the other style-only pedantic lints stay off.

### 14. Small performance fixes (finding 14)

- **`transcript()`:** clones only the rows on screen, not the scrollback
  above them.
- **`wrap()`:** measures each word's width once.
- **`read_frames`:** splits each chunk at newlines and copies whole slices
  instead of pushing one byte at a time.
- **`show_models`:** iterates the catalog page by reference instead of
  cloning the whole catalog.

The existing tests cover the behaviour of all four.

## Order

Each step is its own commit with the full suite green, so any step can be
reverted alone:

1. Hygiene: decisions 8, 9, 10, 13 and the lints.
2. CI (decision 1).
3. Gate crate (decision 2).
4. Duplicates (decision 5).
5. Command registry (decision 3).
6. File splits (decision 6).
7. `App` groups (decision 12).
8. Typed errors (decision 11).
9. Ranking (decision 4).
10. Small performance fixes (decision 14).
11. Test reliability (decision 7).

The registry comes before the split, so the split moves code that is already
smaller. The `App` grouping comes after the split, so its renames land in
files that are already the right size.

## Risks

| Risk | Mitigation |
| --- | --- |
| The `App` regrouping is about 400 mechanical edits | Done in its own commit with no logic change; the full suite plus the live tmux run cover it |
| A typed error's message changes by accident | Existing tests compare the messages; a `Display` test per variant |
| CI on macOS behaves unlike this machine | The first CI run is part of step 2; failures are fixed there |
| The ranking change alters result order | The property test against the old implementation |
