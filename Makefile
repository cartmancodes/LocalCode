.PHONY: help rust-check rust-build tui tui-demo gate

help:
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?##"}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

rust-check: ## Check formatting, tests and lint for the Rust workspace
	scripts/rust-env.sh cargo fmt --all -- --check
	scripts/rust-env.sh cargo test --locked --workspace
	scripts/rust-env.sh cargo clippy --locked --workspace --all-targets -- -D warnings

rust-build: ## Build the optimized terminal UI
	scripts/rust-env.sh cargo build --release --locked -p octet

tui: ## Open the terminal UI with Codex
	scripts/rust-env.sh cargo run --locked -p octet -- --engine codex

tui-demo: ## Preview the terminal UI offline
	scripts/rust-env.sh cargo run --locked -p octet -- --engine demo

ENGINE ?= claude
SCENARIO ?= initialize
gate: ## Run one protocol-gate scenario against a real CLI (ENGINE=claude SCENARIO=simple)
	scripts/rust-env.sh cargo run --locked -p octet-gate --bin protocol-gate -- --engine $(ENGINE) --scenario $(SCENARIO)
