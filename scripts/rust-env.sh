#!/usr/bin/env sh
# Prefer the optional workspace-local toolchain without changing shell profiles.
set -eu
rust_project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -x "$rust_project_root/.superpowers/rust-tools/cargo/bin/cargo" ]; then
    export RUSTUP_HOME="$rust_project_root/.superpowers/rust-tools/rustup"
    export CARGO_HOME="$rust_project_root/.superpowers/rust-tools/cargo"
    export PATH="$CARGO_HOME/bin:$PATH"
fi
if [ "$#" -eq 0 ]; then
    set -- cargo --version
fi
exec "$@"
