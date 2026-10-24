# Synthetic data (owner: data). Included by the root Makefile.
.PHONY: seed seed-csv data-generate data-adversarial data-demo data-splits data-verify data-all data-eval-examples data-public test-data

seed: ## seed the Maple Row universe into $$DATABASE_URL (idempotent; fixed Faker seed)
	$(UV) run invoicedesk-data seed

seed-csv: ## write data/seed/*.csv only (no database)
	$(UV) run invoicedesk-data seed --csv-only

data-generate: ## render synthetic-core (120 docs) to data/generated/ and write data/gold/synthetic-core/
	$(UV) run invoicedesk-data generate

data-adversarial: ## render the 30-case adversarial set (+ priors) and its gold
	$(UV) run invoicedesk-data adversarial

data-demo: ## rebuild the 25-file demo batch in data/demo/ (byte-for-byte reproducible)
	$(UV) run invoicedesk-data demo

data-splits: ## verify the frozen synthetic-core 60 dev / 60 test split
	$(UV) run invoicedesk-data splits

data-verify: ## label QA: route all gold through the rules engine
	$(UV) run invoicedesk-data verify

data-all: ## seed CSVs + synthetic-core + adversarial + demo
	$(UV) run invoicedesk-data all

data-eval-examples: ## load committed gold into eval_examples on $$DATABASE_URL
	$(UV) run invoicedesk-data eval-examples

data-public: ## download SROIE + CORD into data/public/cache (never committed)
	$(UV) run invoicedesk-data public sroie
	$(UV) run invoicedesk-data public cord

test-data: ## data component tests (rendering tests are marked slow)
	$(UV) run pytest data/tests -q
