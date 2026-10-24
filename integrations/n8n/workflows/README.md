# n8n workflows

Two n8n workflow exports connect InvoiceDesk to email (DESIGN M2 and M15). n8n is glue here: all
checks and decisions stay in the InvoiceDesk API and pipeline.

| File | Trigger | Does |
|---|---|---|
| [email-ingest.json](email-ingest.json) | IMAP: new unseen mail in the AP inbox | builds the ingest payload (body + PDF/image attachments, base64), signs it with HMAC-SHA256 and `POST`s it to `/api/v1/ingest/email` |
| [vendor-followup.json](vendor-followup.json) | Webhook `POST /webhook/invoicedesk-followup`, called by the API when a person approves a follow-up draft | validates the payload, sends the email over SMTP (Mailpit in dev), returns 200, or 422 for a bad payload |

Both were written for **n8n 1.80** (the version pinned in `docker-compose.yml` and
`infra/fly/n8n.toml`) and are exported inactive.

## Environment the workflows need

Set these on the n8n container (the compose service and `infra/fly/n8n.toml` show where):

| Variable | Used by | Value |
|---|---|---|
| `INVOICEDESK_API` | email-ingest | API origin, e.g. `http://api:8000` in compose or `http://invoicedesk-api.internal:8000` on Fly |
| `INGEST_WEBHOOK_SECRET` | email-ingest | the **same** secret the API has. Never put it in the workflow JSON. |
| `NODE_FUNCTION_ALLOW_BUILTIN=crypto` | email-ingest | lets the Code node compute the HMAC |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` | both | lets nodes read `$env` (the n8n 1.x default) |
| `FOLLOWUP_FROM_ADDRESS` | vendor-followup | sender address; default `ap@maplerow.example` |
| `FOLLOWUP_ALLOWED_RECIPIENT_DOMAINS` | vendor-followup | optional comma-separated allow-list of recipient domains. If set, anything else gets a 422. |

## Import

1. Open n8n (`http://localhost:5678` with `make up`). On Fly, run
   `fly proxy 5678 -a invoicedesk-n8n`, because n8n has no public service.
2. **Workflows → Import from File**, then choose `email-ingest.json` and `vendor-followup.json`.
   With compose, the files are also mounted read-only at `/workflows`, so you can import them from
   the CLI instead:
   `docker compose exec n8n n8n import:workflow --separate --input=/workflows`.
3. Create the two credentials below and select them in the nodes marked with a warning. The
   exports carry only a placeholder credential reference (`REPLACE_ON_IMPORT`), never secrets.
4. Activate both workflows.
5. Point the API at the follow-up webhook:
   `N8N_FOLLOWUP_WEBHOOK_URL=http://n8n:5678/webhook/invoicedesk-followup` (compose), or
   `http://invoicedesk-n8n.internal:5678/webhook/invoicedesk-followup` (Fly).

## Credentials

| Credential (n8n type) | Node | Dev value | Production |
|---|---|---|---|
| **InvoiceDesk AP inbox (IMAP)** (`imap`) | Vendor inbox (IMAP) | any test mailbox with IMAP (Mailpit has no IMAP server, see below) | the AP mailbox, with an app password or OAuth; TLS on (993) |
| **InvoiceDesk SMTP (Mailpit in dev)** (`smtp`) | Send via SMTP | host `mailpit`, port `1025`, no user, SSL/TLS off | your transactional mail provider; TLS on |

Credentials live in n8n's encrypted credential store (`N8N_ENCRYPTION_KEY`). Never export them
with the workflow.

## email-ingest: what it sends

```jsonc
POST {INVOICEDESK_API}/api/v1/ingest/email
X-InvoiceDesk-Signature: sha256=<hex HMAC-SHA256 of the raw body with INGEST_WEBHOOK_SECRET>
{ "message_id": "<...>", "from": "billing@vendor.example", "subject": "...", "body_text": "...",
  "received_at": "2026-10-20T14:03:00.000Z",
  "attachments": [{ "filename": "inv.pdf", "content_type": "application/pdf", "content_b64": "..." }] }
```

- **Signed bytes = sent bytes.** The Code node serialises the payload once, signs that string,
  and the HTTP Request node sends it as a *raw* body. Re-serialising in between would break the
  signature.
- **Only PDF, JPEG, PNG and HEIC/HEIF attachments are forwarded** (at most 10, each at most 10 MB,
  matching the API limits). Others are dropped and listed under `skipped` in the execution data.
  The API still re-checks type, size and page count.
- **Retries are safe.** The HTTP node retries 3 times. The API stores one record per
  `message_id`, so a redelivery returns the same thread. When a message has no Message-ID, the
  node derives a stable one from sender, subject, date and body.
- **The body is screened too.** The API attaches the email body to each document, and the
  injection classifier and R6 read it, so "please update our bank details" written in the email is
  held for two approvers plus a callback.
- Successful executions are not saved (`saveDataSuccessExecution: none`), so attachments don't
  pile up in n8n's database. Failed executions are kept for debugging; prune them regularly.

**No IMAP mailbox handy?** Send the demo bank-change email exactly as the workflow would:

```bash
INGEST_WEBHOOK_SECRET=... uv run python integrations/n8n/scripts/post_demo_email.py --dry-run
INGEST_WEBHOOK_SECRET=... uv run python integrations/n8n/scripts/post_demo_email.py --api http://localhost:8000
```

## vendor-followup: what it accepts

The API (and the worker's `send_followup` job) `POST`s
`{ "followup_id", "document_id", "to", "subject", "body" }` after a person approves the draft.
The recipient always comes from the **vendor master**, never from the invoice, so a reply can't be
steered to an attacker's address.

The Validate node checks the payload again before anything is sent:

- `to` must be a single address. When an allow-list is set, its domain must be on it.
- `subject` must be non-empty, at most 200 characters, and on one line (no header injection).
- `body` must be non-empty and at most 5,000 characters.
- Runs of 6 or more digits are masked to the last 4.

A bad payload gets `422 {sent: false, errors: [...]}`, which the worker treats as permanent. An
SMTP failure makes the execution fail with a 5xx, which the worker retries.

**Security note.** The API does not sign this webhook call yet (see [SECURITY.md](../../../SECURITY.md)).
Keep n8n unreachable from the internet. The Fly config gives it no public service. Compose
publishes port 5678 on the host for the editor, so only run that stack on a machine that is not
exposed.

## Tests

`uv run pytest integrations/tests` checks:

- both exports parse and are wired correctly;
- they carry no secrets;
- running the Code nodes' JavaScript under Node with a stubbed n8n runtime produces a body and
  signature that the API's HMAC check and `EmailIngest` schema accept;
- the follow-up validator rejects bad payloads.

n8n itself was not run in the build environment ([GAPS.md](../../../docs/GAPS.md)).
