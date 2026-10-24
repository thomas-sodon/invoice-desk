# FastAPI gateway (owner: api). Included by the root Makefile.
.PHONY: api purge test-api

API_HOST ?= 127.0.0.1
API_PORT ?= 8000

api: ## run the API (uvicorn invoicedesk_api.main:app; offline fakes unless INVOICEDESK_MODE=live)
	$(UV) run uvicorn invoicedesk_api.main:app --host $(API_HOST) --port $(API_PORT)

purge: ## delete try-it visitor uploads older than 24h (+ storage objects; audit rows stay)
	$(UV) run python scripts/purge_visitor_uploads.py

test-api: ## API tests (throwaway Postgres; OracleLLM/FakeLLM, FakeAccounting)
	$(UV) run pytest apps/api/tests -q
