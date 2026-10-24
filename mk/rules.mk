# Rules engine R1-R10 (owner: rules). Included by the root Makefile.
.PHONY: test-rules validate

test-rules: ## rules engine tests with the required 100% branch coverage (invariant 7)
	$(UV) run pytest packages/core/tests/rules -q --cov=invoicedesk.rules --cov-branch --cov-fail-under=100 --cov-report=term-missing

validate: ## run R1-R10 on JSON: make validate EX=extraction.json TD=tenant_data.json [ARGS="--raw-conf --today 2026-10-24"]
	@test -n "$(EX)" -a -n "$(TD)" || { echo "usage: make validate EX=extraction.json TD=tenant_data.json [ARGS=...]"; exit 2; }
	$(UV) run invoicedesk validate $(EX) $(TD) $(ARGS)
