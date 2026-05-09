.PHONY: help install dev backend frontend up down logs db-init test lint format

help:
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?##"}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install Python deps (editable) and frontend deps
	python -m pip install -e '.[dev]'
	cd frontend && npm install

up: ## Start postgres via docker compose
	docker compose up -d

down: ## Stop all docker services
	docker compose down

logs: ## Tail logs for the docker stack
	docker compose logs -f --tail=100

db-init: ## Create tables (no migrations yet — uses metadata.create_all)
	python -m backend.app.db_init

backend: ## Run the FastAPI backend with hot reload
	uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8080

frontend: ## Run the Vite frontend dev server
	cd frontend && npm run dev

dev: ## Run backend and frontend together (requires `tmux` or two terminals)
	@echo "Run 'make backend' and 'make frontend' in separate terminals."

test:
	pytest -q

lint:
	ruff check .

format:
	ruff format .
