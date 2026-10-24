# Web app (apps/web, Next.js). Mock mode (no backend) unless NEXT_PUBLIC_API_BASE is set.
.PHONY: web-dev web-build web-fixtures web-screenshots

WEB_PORT ?= 3000
NODE_GLOBAL ?= /opt/node22/lib/node_modules

web-dev: ## Next.js dev server on :3000 (mock mode unless NEXT_PUBLIC_API_BASE is set)
	$(PNPM) --filter web dev

web-build: ## production build of apps/web
	$(PNPM) --filter web build

web-fixtures: ## rebuild the mock-mode fixtures from data/demo (bank numbers masked to last 4)
	$(UV) run python apps/web/scripts/build_mock_fixtures.py

web-screenshots: web-build ## build, start in mock mode, save apps/web/screenshots/*.png (Playwright)
	@set -e; cd apps/web; NEXT_PUBLIC_API_MOCK=1 $(PNPM) exec next start -p $(WEB_PORT) >/dev/null 2>&1 & \
	pid=$$!; trap "kill $$pid" EXIT; \
	for i in $$(seq 1 60); do curl -fs http://localhost:$(WEB_PORT) >/dev/null && break; sleep 1; done; \
	NODE_PATH=$(NODE_GLOBAL) BASE_URL=http://localhost:$(WEB_PORT) node scripts/screenshots.cjs
