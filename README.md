# LocalCode

A native terminal workspace for coding with the official **Claude Code** and
**Codex** CLIs. LocalCode starts the vendor's own binary, streams its replies,
puts every permission request in front of you, and keeps a journal of each
session. It never reads the vendors' credentials: each CLI uses its own login.

It is written in Rust and runs as a single binary, with no browser, server or
Python runtime. It is a working preview; see [docs/tui.md](docs/tui.md) for
what is implemented and what is pending.

## Prerequisites

- The Rust toolchain pinned in `rust-toolchain.toml` and a native C linker.
  `scripts/rust-env.sh` uses a workspace-local toolchain when one is installed.
- For real sessions, `claude` and/or `codex` on `PATH`, already logged in.
  `--engine demo` needs neither.

## Build and run

```sh
make rust-build
./target/release/localcode --engine demo
./target/release/localcode --engine claude --cwd /path/to/project
./target/release/localcode --engine codex --cwd /path/to/project
```

`make tui-demo` builds and opens the offline demo; `make tui` opens Codex.

Useful options: `--model MODEL`, `--mode ask|accept-edits|auto|full-access`,
`--resume VENDOR_SESSION_ID`, `--binary PATH` (a specific CLI) and
`--journal-dir PATH`. `localcode --help` lists them all.

To install for your user:

```sh
scripts/rust-env.sh cargo install --locked --path crates/localcode --root "$HOME/.local"
```

## Using it

| Key | Action |
| --- | --- |
| Enter | Send the prompt |
| Alt+Enter, Ctrl+J | New line |
| Esc | Cancel the running turn |
| Shift+Tab | Cycle permission mode: ask → accept-edits → auto |
| A / D | Allow once / deny a permission request |
| Ctrl+P | Command palette |
| F1 | Help |
| Ctrl+Q | Quit |

Commands: `/model` (switch model or provider), `/mode` (permission mode),
`/goal` (a multi-turn objective), `/session`, `/new`, `/reconnect`,
`/export`. In `auto` mode each vendor's own reviewer decides approvals;
`full-access` has to be typed and turns every check off. The full key and
command reference, permission-mode mapping and limits are in
[docs/tui.md](docs/tui.md).

## Layout

| Crate | Role |
| --- | --- |
| `crates/lc-proc` | Bounded JSON-line transport and process-group supervision for vendor CLIs |
| `crates/lc-store` | Append-only session journals |
| `crates/lc-engine` | The vendor driver (`live`) and the protocol gate used to collect live contract evidence |
| `crates/lc-core` | Session boundary, model selection and persistent goals |
| `crates/lc-tui` | The terminal interface |
| `crates/localcode` | The `localcode` binary and its terminal and credential-guard tests |
| `crates/lc-testkit` | Scripted fake vendors for tests |

## Development

```sh
make rust-check   # format check, all tests, clippy with warnings as errors
```

`crates/localcode/tests/credentials.rs` fails the build if any crate names a
vendor credential store or sets a secret-looking environment variable.

## Documentation

- [docs/tui.md](docs/tui.md) — running the terminal UI, keys, commands, modes and limits
- [docs/rust/](docs/rust/) — goals, the feature-parity matrix and plans
- [docs/superpowers/](docs/superpowers/) and [docs/reviews/](docs/reviews/) — dated design specs, plans and reviews

An earlier Python harness and web UI were removed on 2026-10-04; they remain in
git history (see [docs/rust/parity-matrix.md](docs/rust/parity-matrix.md)).
