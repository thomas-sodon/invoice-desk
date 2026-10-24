# Offline end-to-end: the 25-invoice demo + the bank-change e-mail through API, pipeline and
# worker on a throwaway Postgres (OracleLLM, FakeAccounting; no credentials, $0).
.PHONY: test-e2e

test-e2e: ## offline end-to-end demo run (DESIGN §6.2 board 18/4/3, R6 holds, idempotent posting)
	$(UV) run pytest tests/e2e -q
