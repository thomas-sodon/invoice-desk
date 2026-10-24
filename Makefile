# InvoiceDesk Makefile. Only targets that work live here; components add theirs in mk/<component>.mk
# (included below) so they never edit this file. Keep CLAUDE.md's command list true.
SHELL := /bin/bash
UV ?= uv
PNPM ?= pnpm
PY_PATHS := packages apps/api services evals data scripts conftest.py
COMPOSE ?= docker compose

.PHONY: help install lint format typecheck test test-py test-web test-pg cov up down logs migrate audit-verify

help: ## list targets
	@grep -hE '^[a-zA-Z0-9_.-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

install: ## uv sync (Python workspace) + pnpm install when apps/web exists
	$(UV) sync
	@if [ -f apps/web/package.json ]; then $(PNPM) install; fi

lint: ## ruff lint + format check (+ web lint when present)
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	@if [ -f apps/web/package.json ]; then $(PNPM) --filter web lint; fi

format: ## ruff format + autofix
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## pyright (+ web tsc when present)
	$(UV) run pyright
	@if [ -f apps/web/package.json ]; then $(PNPM) --filter web typecheck; fi

test-py: ## pytest (Postgres tests start a throwaway cluster or skip)
	$(UV) run pytest

test-web: ## web typecheck + lint (+ unit tests when present)
	@if [ -f apps/web/package.json ]; then $(PNPM) --filter web typecheck && $(PNPM) --filter web lint && $(PNPM) --filter web test --if-present; else echo "apps/web not built yet"; fi

test: test-py test-web ## pytest + web typecheck/lint

cov: ## pytest with branch coverage report for packages/core
	$(UV) run pytest --cov=invoicedesk --cov-branch --cov-report=term-missing

up: ## docker compose: Postgres, api, worker, mcp, n8n, Mailpit
	$(COMPOSE) up -d --build

down: ## stop the compose stack
	$(COMPOSE) down

logs: ## follow compose logs
	$(COMPOSE) logs -f --tail=100

migrate: ## apply SQL migrations to $$DATABASE_URL
	$(UV) run invoicedesk migrate

audit-verify: ## recompute the audit-log hash chain on $$DATABASE_URL
	$(UV) run invoicedesk audit-verify

-include mk/*.mk
