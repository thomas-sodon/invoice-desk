# QuickBooks Online sandbox adapter, OAuth bootstrap, demo reset (owner: accounting).
.PHONY: demo-reset qbo-bootstrap test-accounting

demo-reset: ## delete sandbox demo bills, clear demo batches, re-seed (dry run; APPLY=1 to do it, ARGS="--skip-qbo|--sweep")
	$(UV) run python scripts/demo_reset.py $(if $(APPLY),--apply,) $(ARGS)

qbo-bootstrap: ## one-time QBO sandbox OAuth; prints QBO_REALM_ID / QBO_REFRESH_TOKEN for .env
	$(UV) run python scripts/qbo_oauth_bootstrap.py $(ARGS)

test-accounting: ## accounting adapter tests (respx-mocked QBO; Postgres tests use a throwaway cluster)
	$(UV) run pytest packages/core/tests/accounting -q
