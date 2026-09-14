.PHONY: help install dev backend frontend up down logs status test lint typecheck format soak codex-schema

help:
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?##"}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# Every Python tool below is run out of `.venv/bin/` — the same venv
# `setup.sh` creates — rather than off PATH. `make test` used to call bare
# `pytest`, which fails ("command not found", or worse, finds a DIFFERENT
# interpreter's pytest with none of these dependencies) unless the venv happens
# to be activated in the calling shell. The one command a contributor is told
# to run was the one that did not work.

install: ## Create the venv, install Python deps (editable) and frontend deps
	python3 -m venv .venv
	.venv/bin/python -m pip install -e '.[dev]'
	cd frontend && npm install

# The stack is purely host-side: the vendor CLIs authenticate through a browser,
# which does not work from inside a container, so there is nothing to compose.
# These targets used to call `docker compose` and `backend.app.db_init`; neither
# a compose file nor that module has ever existed in this tree, so all four
# failed on contact. They delegate to `setup.sh`, which is what actually runs
# the stack.

up: ## Start backend + frontend (setup.sh)
	./setup.sh

down: ## Stop backend + frontend
	./setup.sh stop

logs: ## Tail backend + frontend logs
	./setup.sh logs

status: ## Show which services are running
	./setup.sh status

backend: ## Run the FastAPI backend with hot reload
	.venv/bin/uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8080

frontend: ## Run the Vite frontend dev server
	cd frontend && npm run dev

dev: ## Run backend and frontend together (requires `tmux` or two terminals)
	@echo "Run 'make backend' and 'make frontend' in separate terminals."

test: ## Run the backend test suite
	.venv/bin/pytest -q

lint: ## Lint the tree (ruff)
	.venv/bin/ruff check .

typecheck: ## Type-check the backend (mypy)
	.venv/bin/mypy backend/app

# The soak, plus everything else, under the two conditions the default run
# does not impose: warnings are errors (a `slow` marker that stops being
# registered, a deprecation the vendor SDK starts emitting), and every test has
# a hard wall clock. The timeout exists because of a hang observed exactly once
# during Task 5 — the third test in collection order, under `-W error`, with a
# second pytest running beside it — which 8 bounded reruns never reproduced. A
# hang that rare is only ever caught by a run that cannot hang: on expiry the
# watchdog dumps every thread's stack (faulthandler) and fails the test, so the
# next occurrence arrives with evidence instead of a stopped CI job.
# `-s` is load-bearing twice over: without it pytest captures stdout/stderr per
# test, so the soak's measurements never reach the log on a PASSING run (the
# whole point of printing them), and a wedged test's stack dump dies with the
# captured buffer at the moment the run is aborted.
soak: ## Run the whole suite including the soak, warnings-as-errors, per-test timeout
	LOCALCODE_TEST_TIMEOUT_S=300 .venv/bin/pytest -q -s -W error::UserWarning -m 'not requires_cli'

format: ## Auto-format the tree (ruff)
	.venv/bin/ruff format .

codex-schema: ## Dump the codex app-server JSON schema for reconciliation
	codex app-server generate-json-schema > docs/codex-app-server.schema.json
