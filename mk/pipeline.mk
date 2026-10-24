# LangGraph pipeline + worker (owner: pipeline). Included by the root Makefile.
.PHONY: worker worker-once test-pipeline

worker: ## run the pipeline worker (Postgres job queue, LangGraph + PostgresSaver, concurrency 8)
	$(UV) run invoicedesk-worker

worker-once: ## drain the job queue once and exit (requeues stale jobs first)
	$(UV) run invoicedesk-worker --once

test-pipeline: ## pipeline + worker tests (throwaway Postgres; FakeLLM, FakeAccounting)
	$(UV) run pytest packages/core/tests/pipeline services/worker/tests -q
