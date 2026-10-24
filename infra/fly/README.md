# Fly.io deployment

Four Fly apps run the backend. The web app goes to Vercel, and Postgres, Storage and Auth come
from Supabase (DESIGN §4.4).

| Config | App | Image | Public? | Process |
|---|---|---|---|---|
| [api.toml](api.toml) | `invoicedesk-api` | [`infra/docker/python.Dockerfile`](../docker/python.Dockerfile) | yes (`https://invoicedesk-api.fly.dev`) | `uvicorn invoicedesk_api.main:app`; release command `invoicedesk migrate` |
| [worker.toml](worker.toml) | `invoicedesk-worker` | same | no | `invoicedesk-worker` |
| [mcp.toml](mcp.toml) | `invoicedesk-mcp` | same | yes (`https://invoicedesk-mcp.fly.dev/mcp`) | `invoicedesk-mcp` |
| [n8n.toml](n8n.toml) | `invoicedesk-n8n` | `n8nio/n8n:1.80.0` | **no** (editor via `fly proxy`) | n8n |

> **Status:** these configs parse and follow Fly's documented `fly.toml` format, but nothing has
> been deployed from the build environment ([GAPS.md](../../docs/GAPS.md)).

## First deploy

Run everything from the **repo root**. The Python apps need the whole uv workspace as build
context. `dockerfile` paths in the configs are relative to the config file.

```bash
fly apps create invoicedesk-api
fly apps create invoicedesk-worker
fly apps create invoicedesk-mcp
fly apps create invoicedesk-n8n
fly volumes create n8n_data --region iad --size 1 -a invoicedesk-n8n

# set secrets (below), then:
fly deploy . --config infra/fly/api.toml      # runs `invoicedesk migrate` as the release command
fly deploy . --config infra/fly/worker.toml
fly deploy . --config infra/fly/mcp.toml
fly deploy --config infra/fly/n8n.toml
```

> **Before the first deploy, add a root `.dockerignore`.** The shared Dockerfile ends with
> `COPY . .` and the build context is the repo root. The repo has no `.dockerignore` yet, so a
> local `.env` (every secret listed below), `node_modules`, `.venv` and generated data would be
> copied into the image and pushed to Fly's registry. Exclude at least `.env`, `.env.*` (keep
> `.env.example`), `.git`, `.venv`, `node_modules`, `apps/web/.next`, `var/` and
> `data/public/cache` ([GAPS.md](../../docs/GAPS.md)).

The services reach each other over Fly's private network (`<app>.internal`, IPv6). That is why
the API and n8n listen on `::`.

## Secrets

Secrets are set with `fly secrets set` (or `fly secrets import < file`, from a file that is never
committed) and are **never** written into these configs or the repo. Values come from your local
`.env` (see [`.env.example`](../../.env.example)).

```bash
# API
fly secrets set -a invoicedesk-api \
  DATABASE_URL=... ANTHROPIC_API_KEY=... \
  SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... SUPABASE_JWT_SECRET=... \
  QBO_CLIENT_ID=... QBO_CLIENT_SECRET=... QBO_REALM_ID=... QBO_REFRESH_TOKEN=... \
  LANGFUSE_HOST=... LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... \
  TURNSTILE_SECRET_KEY=... SESSION_SIGNING_SECRET=... INGEST_WEBHOOK_SECRET=... AGENT_JWT_SECRET=...

# Worker
fly secrets set -a invoicedesk-worker \
  DATABASE_URL=... ANTHROPIC_API_KEY=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
  QBO_CLIENT_ID=... QBO_CLIENT_SECRET=... QBO_REALM_ID=... QBO_REFRESH_TOKEN=... \
  LANGFUSE_HOST=... LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=...

# MCP server (the issuer is a hosted OAuth provider with CIMD support; don't hand-roll OAuth)
fly secrets set -a invoicedesk-mcp \
  AGENT_JWT_SECRET=... MCP_AUTH_ISSUER=https://<tenant>.authkit.app MCP_REQUEST_STATE_KEYS=...

# n8n
fly secrets set -a invoicedesk-n8n N8N_ENCRYPTION_KEY=... INGEST_WEBHOOK_SECRET=...
```

Notes:

- `INGEST_WEBHOOK_SECRET` must be identical on the API and n8n. `AGENT_JWT_SECRET` must be
  identical on the API and the MCP server. Generate each with `openssl rand -hex 32`.
- `QBO_*` must be **sandbox** credentials. The adapter refuses any other environment and host.
- `DATABASE_URL` must point to Supabase's **direct or session-mode** connection (port 5432). The
  board's live updates use `LISTEN/NOTIFY` and LangGraph holds session state, so neither works
  through the transaction pooler.
- Rotate a secret with `fly secrets set` again. Fly restarts the machines with the new value.
  `QBO_REFRESH_TOKEN` rotates on its own: the token manager stores each rotated token encrypted
  in Postgres, so the secret only seeds the first refresh.

## Web (Vercel)

Deploy `apps/web` as a Vercel project with root directory `apps/web` and these settings:

- `NEXT_PUBLIC_API_BASE=https://invoicedesk-api.fly.dev`
- `NEXT_PUBLIC_TURNSTILE_SITE_KEY=<site key>`

Add the Vercel origin to the API's `WEB_ORIGINS`. Without `NEXT_PUBLIC_API_BASE`, the site runs
in its offline mock mode.

## Scheduled jobs

The try-it purge (`scripts/purge_visitor_uploads.py`, `make purge`) must run at least hourly. A
Fly scheduled machine on the deployed API image can do this. Take the image reference from
`fly image show -a invoicedesk-api`. This command is untested here.

```bash
fly machine run --schedule hourly -a invoicedesk-api \
  registry.fly.io/invoicedesk-api:<deployment-tag> python scripts/purge_visitor_uploads.py
```

## Tests

`uv run pytest integrations/tests` parses these configs with `tomllib` and checks that:

- every Python app builds from the shared Dockerfile;
- the API runs migrations on release, and uvicorn never trusts client-supplied
  `X-Forwarded-For` (`--no-proxy-headers`, `TRUST_FORWARDED_FOR=false`);
- n8n has no public service;
- no secret-looking key appears in any `[env]` table.
