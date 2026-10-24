# Eval harness, CI gate, Report Card (owner: evals). Included by the root Makefile.
# Offline runs (INVOICEDESK_MODE=offline, the default) use the gold-backed oracle and cost $0.
# Live runs print a cost estimate and need EVAL_FLAGS=--i-know-this-costs-money; the full matrix
# (CONFIG=all, ~$40) also needs --full-matrix. Use CONFIG=Dp for D' (no quote needed).
.PHONY: eval eval-offline eval-smoke eval-fit-thresholds eval-estimate report-card test-evals

CONFIG ?= D
SPLIT ?= test
REPEATS ?= 3
EVAL_FLAGS ?=
EVAL_DSN ?=

eval: ## run eval CONFIG=D [SPLIT=test REPEATS=3 EVAL_FLAGS=...]; costs real money when live
	$(UV) run invoicedesk-eval run --config "$(CONFIG)" --split "$(SPLIT)" --repeats $(REPEATS) \
		$(if $(EVAL_DSN),--dsn "$(EVAL_DSN)",) $(EVAL_FLAGS)

eval-offline: ## offline ($0) D + N on dev+adversarial (n=3), report + Report Card PDF
	$(UV) run invoicedesk-eval run --offline --config D,N --split dev+adversarial --repeats 3 \
		--report-card $(if $(EVAL_DSN),--dsn "$(EVAL_DSN)",)

eval-smoke: ## offline smoke set + the PR gate against evals/baselines/smoke-offline.json
	$(UV) run invoicedesk-eval run --offline --config D --split smoke --repeats 1 \
		--out-dir var/eval --stem smoke-offline $(if $(EVAL_DSN),--dsn "$(EVAL_DSN)",)
	$(UV) run invoicedesk-eval gate --config D --current var/eval/smoke-offline.json \
		--baseline evals/baselines/smoke-offline.json

eval-fit-thresholds: ## choose tau_field/tau_hi on the DEV split only (never test)
	$(UV) run invoicedesk-eval fit-thresholds --split dev --config D $(EVAL_FLAGS)

eval-estimate: ## print the live cost estimate for CONFIG/SPLIT/REPEATS
	$(UV) run invoicedesk-eval estimate --config "$(CONFIG)" --split "$(SPLIT)" --repeats $(REPEATS)

report-card: ## render docs/report-card/AP-Automation-Report-Card.pdf from the latest eval run
	$(UV) run invoicedesk-eval report-card $(if $(EVAL_DSN),--dsn "$(EVAL_DSN)",)

test-evals: ## evals unit tests (+ Postgres harness tests, marked slow)
	$(UV) run pytest evals/tests -q
