.PHONY: help install dev backend frontend up down logs db-init test lint typecheck format codex-schema

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

up: ## Start postgres via docker compose
	docker compose up -d

down: ## Stop all docker services
	docker compose down

logs: ## Tail logs for the docker stack
	docker compose logs -f --tail=100

db-init: ## Create tables (no migrations yet — uses metadata.create_all)
	.venv/bin/python -m backend.app.db_init

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

format: ## Auto-format the tree (ruff)
	.venv/bin/ruff format .

codex-schema: ## Dump the codex app-server JSON schema for reconciliation
	codex app-server generate-json-schema > docs/codex-app-server.schema.json
