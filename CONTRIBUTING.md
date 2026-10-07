# Contributing to Octet

Thanks for your interest. Bug reports, fixes, documentation and features are
all welcome. For anything larger than a small fix, open an issue first so we
can agree on the approach before you spend time on it.

## Set up

You need macOS or Linux, [rustup](https://rustup.rs) and a C linker. The
toolchain is pinned in `rust-toolchain.toml`; rustup installs it on the first
build.

```sh
git clone https://github.com/cartmancodes/octet.git
cd octet
make tui-demo      # build and open the offline demo
make rust-build    # optimised binary in target/release/octet
```

The `make` targets run cargo through `scripts/rust-env.sh`. It uses a
toolchain under `.superpowers/rust-tools/` when one is there (that folder is
git-ignored) and your normal rustup toolchain otherwise, so you don't need to
set anything up for it.

To try a change against a real vendor, install `claude` or `codex`, log in
with the vendor's own command, and run
`./target/release/octet --engine claude` (or `codex`).

## Check your change

```sh
make rust-check
```

This is the gate every change must pass. It runs, in order:

1. `cargo fmt --all -- --check`
2. `cargo clippy --locked --workspace --all-targets -- -D warnings`
3. `cargo doc --locked --workspace --no-deps`, with warnings as errors
4. `cargo test --locked --workspace`

CI also checks licences, banned crates and sources with `cargo-deny`
(`deny.toml`), and audits dependencies weekly.

Clippy also enforces the house Rust rules, set as workspace lints in
`Cargo.toml`: no `unwrap()` outside tests (use `?` or `expect` with the reason
it cannot fail), a `// SAFETY:` comment on every `unsafe` block, docs on every
public item with `# Errors` and `# Panics` sections where they apply, and
borrowing over taking ownership you do not need. `clippy::pedantic` is on, as
are `missing_debug_implementations` and `unreachable_pub` (crate-only items
are `pub(crate)`). An exception says why, as
`#[expect(clippy::lint, reason = "…")]`; a bare `#[allow]` fails the check.
Crates without `unsafe` code forbid it. The full guidance is the
[rust-engineer skill](.claude/skills/rust-engineer/SKILL.md), which Claude Code
loads when it works on this repository; its "In this repository" section
lists what applies here.

The tests include real pseudoterminal tests in `crates/octet/tests/terminal.rs`
that start the `octet` binary and read its screen, so run them in a normal
terminal session. Tests marked `#[ignore]` either need the real vendor CLIs
and an account, or are manual diagnostics. Run one explicitly when your change
affects it:

```sh
scripts/rust-env.sh cargo test -p octet --test terminal -- --ignored <name>
```

## Ground rules

These keep Octet safe to point at a real codebase. Changes that break them
will not be merged.

- **Never read vendor credentials.** Octet starts the vendor CLI and lets it
  use its own login. It must not read, copy, store or forward credential
  files, keychains or tokens. `crates/octet/tests/credentials.rs` fails the
  build if a crate names a vendor credential store or sets a secret-looking
  environment variable.
- **Approvals fail closed.** Octet never answers a vendor's permission request
  by itself. Anything expired, cancelled, too large to show, over the cap of
  eight waiting, or not understood is denied.
- **Stay bounded.** Queues, buffers, journals and the visible transcript all
  have limits (listed in [docs/tui.md](docs/tui.md)). New data paths need a
  limit too, and an overload must stop the session visibly rather than drop
  output silently. (The one cut, a reply over 2 MiB, ends with a note.)
- **Journals are private.** They hold prompts, source code and tool output.
  Keep their files at mode 0600 and never overwrite one.

## Tests

Every behaviour change needs a test that fails without it. Where to put one:

- Unit tests sit next to the code (`#[cfg(test)] mod tests`).
- End-to-end terminal behaviour goes in `crates/octet/tests/terminal.rs`.
- Vendor protocol behaviour uses the scripted fake vendors in
  `crates/octet-testkit`; no test may need a network connection or an
  account unless it is `#[ignore]`d.

## Documentation

Update the docs in the same change as the code:

- [docs/tui.md](docs/tui.md) for keys, commands, options and limits;
- the feature list in [README.md](README.md) and
  [docs/rust/tui-features.md](docs/rust/tui-features.md) for anything a user
  would notice;
- [CHANGELOG.md](CHANGELOG.md), under the version in progress.

Write for someone using Octet, in plain sentences. `docs/superpowers/` and
`docs/reviews/` are dated working notes; add to them if you like, but they
don't replace the user docs.

## Commits and pull requests

- Branch from `master`.
- Give each commit one logical change. Write the subject in the imperative
  ("Add …", "Fix …"), around 60 characters at most, and use the body to
  explain why.
- Open a pull request that says what changed and why, and how you tested it.
  Link the issue it addresses. Make sure `make rust-check` passes.

## Reporting bugs

Use the bug report form on GitHub. Include the Octet commit
(`git rev-parse --short HEAD`), the engine, your vendor CLI version
(`claude --version` or `codex --version`), the OS and the terminal. If you
attach part of a journal, remove any code, prompts or paths you don't want to
be public first.

If you find a security problem, such as a way for Octet to expose credentials
or answer an approval without you, please don't open a public issue. Report
it privately through GitHub's **Security → Report a vulnerability**.

## License

Octet is licensed under either of the [MIT](LICENSE-MIT) or
[Apache 2.0](LICENSE-APACHE) licenses, at your option. Unless you explicitly
state otherwise, any contribution intentionally submitted for inclusion in
Octet by you, as defined in the Apache-2.0 license, shall be dual licensed as
above, without any additional terms or conditions.
