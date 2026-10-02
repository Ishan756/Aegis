# ---- setup -------------------------------------------------------------------

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.env: ## Create .env from the template if missing
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")

# Backend: uv if present, otherwise plain venv + pip.
UV := $(shell command -v uv 2> /dev/null)
VENV_PY := backend/.venv/bin/python

setup: .env ## Install backend and frontend dependencies
	@if [ -n "$(UV)" ]; then \
		$(UV) venv backend/.venv && \
		$(UV) pip install --python $(VENV_PY) -e "backend[dev]"; \
	else \
		python3 -m venv backend/.venv && \
		$(VENV_PY) -m pip install --upgrade pip && \
		$(VENV_PY) -m pip install -e "backend[dev]"; \
	fi
	cd frontend && npm install
	@echo "Setup complete. Run 'make dev-backend' and 'make dev-frontend'."

# ---- run ---------------------------------------------------------------------

dev-backend: ## Start the FastAPI server with reload on :8000
	cd backend && .venv/bin/python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-frontend: ## Start the Vite dev server on :5173
	cd frontend && npm run dev

# ---- quality -----------------------------------------------------------------

test: test-backend test-frontend ## Run all tests

test-backend: ## Run backend unit and API tests
	cd backend && .venv/bin/python -m pytest

test-frontend: ## Run frontend component tests
	cd frontend && npm test

smoke: ## Run smoke tests against a running backend
	backend/.venv/bin/python -m pytest tests -q

lint: ## Lint backend and frontend
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .
	cd frontend && npm run lint

format: ## Auto-format the backend
	cd backend && .venv/bin/ruff check --fix . && .venv/bin/ruff format .

typecheck: ## Typecheck the frontend
	cd frontend && npm run typecheck

build: ## Build the production frontend bundle
	cd frontend && npm run build

check: lint typecheck test ## Run lint, typecheck and tests

# ---- docker ------------------------------------------------------------------

docker-up: ## Start the Docker stack on :8080 and :8000
	docker compose up --build

docker-down: ## Stop the Docker stack
	docker compose down

docker-logs: ## Follow Docker logs
	docker compose logs -f

clean: ## Remove build output and caches
	rm -rf frontend/dist frontend/node_modules/.tmp
	find backend -name '__pycache__' -type d -prune -exec rm -rf {} +

.PHONY: help setup dev-backend dev-frontend test test-backend test-frontend smoke \
	lint format typecheck build check docker-up docker-down docker-logs clean