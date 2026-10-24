# Remote MCP server (owner: mcp). Included by the root Makefile.
.PHONY: mcp mcp-devtoken test-mcp

mcp: ## run the MCP server (Streamable HTTP, stateless; needs MCP_AUTH_ISSUER + AGENT_JWT_SECRET, see .env.example)
	$(UV) run invoicedesk-mcp

mcp-devtoken: ## offline only: write var/mcp-dev keys/JWKS and print MCP_AUTH_* env lines + a bearer token
	$(UV) run python -m invoicedesk_mcp.devkeys $(if $(SCOPES),--scopes "$(SCOPES)")

test-mcp: ## MCP server tests (in-process API on throwaway Postgres, test JWKS, offline fakes)
	$(UV) run pytest services/mcp-server/tests -q
