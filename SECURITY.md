# Security

InvoiceDesk reads documents written by outsiders and can create bills in an accounting system.
This page covers what it protects, who it protects against, the controls in place, how they were
tested, and what a production deployment would still need. The design rationale is in
[ADR-0005](docs/adr/0005-guardrails.md).

**Reporting a vulnerability.** Please open a private security advisory on the GitHub repository
(Security -> Report a vulnerability) rather than a public issue. This is a portfolio project
that runs against a QuickBooks *sandbox* with fictional data only; there is no production data
to expose.

## Threat model

### Assets

| Asset | Why it matters |
|---|---|
| Where money goes (vendor remit-to and bank details) | The main target. A changed bank account diverts real payments. |
| The decision to post a bill | A wrong post means a duplicate or inflated payment. |
| The accounting connection (QBO OAuth tokens) | Abuse could create bills or read company data. |
| Invoice contents and vendor data | Business-confidential. Bank and account numbers are sensitive. |
| The audit trail | Needed to prove who approved what. |
| LLM spend | The public page can be used to burn money. |

### Adversaries and entry points

| Adversary | Entry point | Goal |
|---|---|---|
| Fraudulent or compromised vendor | Invoice files (PDF, photo, scan) and email bodies | Change the remit-to account; get a duplicate or inflated bill paid; inject instructions into the AI |
| Impersonator | Lookalike letterhead ("Rapid Rotor" vs "Rapid Rooter", homoglyphs) | Get paid as a known vendor |
| Malicious insider or one compromised staff account | Upload plus approval UI | Approve their own fraudulent upload |
| Compromised or overeager AI agent | MCP tools | Post a bill it should not, or clear a hold |
| Anonymous internet visitor | Public try-it page | Run up LLM spend, store abusive content, reach the accounting system |
| Network attacker | n8n ingest webhook, API | Forge an inbound email, replay requests |

### Trust boundaries

1. **Untrusted content → the reader.** Document text and images reach exactly one kind of
   component: the tool-less extractor and the classifier. Their output is schema-validated JSON
   that code treats as data.
2. **Reader → decision code.** Rules R1-R10 and `status.transition()` decide. No model output is
   evidence for a status change.
3. **Decision code → accounting.** A posting adapter with two operations and no bank fields.
4. **Agents → API.** The MCP server gets values, rule results and counts, never raw document
   text, and calls the API with short-lived, single-scope tokens.
5. **Anonymous visitors → everything else.** Separate session auth, separate limits, and no path
   to Posted.

## Controls, mapped to the OWASP Top 10 for LLM Applications (2026)

The mapping follows DESIGN §5.5, which used the Cloud Security Alliance summaries of the 2026
list ([overview](https://cloudsecurityalliance.org/articles/the-owasp-top-10-for-llm-applications-2026-what-changed-and-why-it-matters),
[research note](https://labs.cloudsecurityalliance.org/research/csa-research-note-owasp-llm-top10-2026-incident-weighted-202/)).
Risk numbers are given only where the spec names them. Check the numbering against the primary
OWASP list before quoting it elsewhere.

| Layer | Control | Where | OWASP LLM 2026 risk |
|---|---|---|---|
| Pre-LLM | File type and size allow-list (JPEG/PNG/HEIC/PDF, at most 10 MB and 5 pages); PDF sanitize (JavaScript, embedded files and forms stripped; fails closed); EXIF and GPS strip | `invoicedesk/preprocess/`, `apps/api/.../uploads.py` | Supply chain and input handling |
| Pre-LLM | **Hidden-text detector:** text-layer words off-page, under 4 pt, within ΔE < 5 of the background, or absent from the OCR of the render | `invoicedesk/guardrails/hidden_text.py` | **#1 Prompt injection** (indirect, cross-modal) |
| Pre-LLM | **Faint-text detector:** text only a CLAHE-enhanced OCR pass can read | `invoicedesk/guardrails/faint_text.py` | #1 (instructions hidden in images) |
| Pre-LLM | **Injection classifier** (Haiku 5.5) over OCR text, the text layer, recovered hidden text and the email body. It fails closed (quarantine) when it cannot answer. | `invoicedesk/guardrails/classifier.py` | #1 |
| Pre-LLM | **Quarantine (R10):** a flagged document goes Quarantined → Flagged and is never sent to the extractor | `contracts/screening.should_quarantine`, `pipeline/graph.py` | #1 |
| LLM | **Tool-less extractor** (dual-LLM / quarantined-reader pattern): untrusted-data framing, schema-only output (`extra="forbid"`), `embedded_instructions` captured as data, no tools and no `tool_choice` | `invoicedesk/extract/` | #1, Excessive agency |
| Post-LLM | **Deterministic rules R1-R10** as pure functions with 100% branch coverage. R6 (bank change) and R10 (injection) have no config switch. | `invoicedesk/rules/`, `tenants/*/rules.yaml` | **#3 Excessive agency** |
| Post-LLM | **Evidence-gated state machine:** ReadyToPay needs a recomputed `RoutingDecision`; Flagged → ReadyToPay needs two distinct approvers (not the uploader) plus a callback; `llm` actors are always refused | `invoicedesk/status.py`, `db/repo.set_status`, DB triggers | #3 |
| Action | **Least-privilege posting:** only `create_bill` and `attach_file`; no BillPayment, vendor edits or bank fields; sandbox host only; `requestid` idempotency on every write | `invoicedesk/accounting/` | #3 |
| Action | **Agent facade:** three tools with separate scopes (`invoices:write`, `invoices:read`, `bills:post`); `post_bill` refuses non-ReadyToPay (always Flagged) before asking the human, who must type the vendor and total, and the API compares them with the stored values | `services/mcp-server/`, `apps/api/.../routes/agent.py` | #3 |
| Ops | Turnstile, per-IP and per-session limits, a global $3/day spend cap with a replay fallback, token caps per call, page limits, per-tier semaphores | `apps/api/.../{limits,turnstile}.py`, `config/models.py` | **#6 Unbounded consumption** |
| Data | Bank and account numbers masked to the last 4 in logs, traces, the audit log, API responses and the UI. Remit-to details and the system prompt never reach the posting payload or anonymous callers. | `invoicedesk/masking.py`, `telemetry/`, `apps/web/lib/mask.ts` | **#8 Hidden context exposure** |
| Audit | Append-only, hash-chained `audit_log` (SHA-256 of the previous row). A DB trigger forbids UPDATE, DELETE and TRUNCATE. Every state change, detector verdict, rule result and human action writes a row. | `invoicedesk/audit/`, `make audit-verify` | Accountability |

### The dual-LLM pattern, concretely

```text
untrusted file/email ──> [screeners] ──> [extractor: Claude, NO tools] ──> InvoiceExtraction JSON
                                                                              │ (data only)
                                                                              v
                      [rules R1-R10 + status.transition(): plain Python] ──> ReadyToPay / NeedsReview / Flagged
                                                                              │
                      [posting adapter: create_bill + attach_file only] <──── only from ReadyToPay
```

A successful injection can, at worst, put wrong *values* into the JSON. Those values still have
to pass the vendor master, work order, estimate, duplicate, arithmetic, cap and date rules. A
remit-to change, an instruction addressed to an AI, or a `bank_or_remit_change` classification
fails R6 and stops at Flagged. That is containment, not detection: it holds even when the
classifier misses ([Willison, dual LLM pattern](https://simonwillison.net/2023/Apr/25/dual-llm-pattern/);
[CaMeL](https://arxiv.org/abs/2503.18813)).

### Bank-change holds (R6): two people plus a callback

- R6 fails when the remit-to fingerprint (`invoicedesk.remit.remit_fingerprint`, a salted hash)
  differs from the vendor master, when the classifier reports `bank_or_remit_change`, or when
  `embedded_instructions` is non-empty. It checks the invoice *and* the email body.
- Release needs `ApprovalEvidence`: **two distinct staff approvers, neither of them the uploader,
  with a callback attestation** (a call to the phone number already on file, never one from the
  document). The rule lives in three places: `status.transition()`, the API (`403 self_approval`),
  and database triggers (approver ≠ uploader; `callback_attested` is required on every
  `bank_change` approve).
- No system, MCP or LLM actor can produce this transition, and there is no config flag for it.

### Least-privilege posting

- The public surface of `QuickBooksAdapter` is exactly `{system, create_bill, attach_file}`, and a
  test asserts it. Another test asserts that every HTTP call is POST bill, POST upload, or GET
  query on Bill, and that the module contains no payment, vendor or delete endpoints.
- `BillRequest`/`BillLine` reject unknown fields, so bank, remit-to and payment fields can't be
  passed. Account numbers in memos are masked before sending.
- Every write carries `requestid`. A retry after a timeout returns the original bill. If QBO's
  requestid cache has expired, a duplicate-DocNumber error triggers a lookup that accepts only a
  bill carrying this key's marker.
- The transport refuses every environment except `sandbox` and every host except
  `sandbox-quickbooks.api.intuit.com`. OAuth refresh tokens are stored encrypted (pgcrypto).

### Audit chain

Each `audit_log` row stores `prev_hash` (the previous row's hash) and
`row_hash = SHA-256(canonical JSON of {ts, actor, tenant_id, document_id, action, before, after, prev_hash})`.
Payloads are masked *before* hashing, so the chain never contains a full account number.
`make audit-verify` (and `GET /api/v1/documents/{id}/audit`, which returns `chain_ok`) recompute
it. Demo reset deletes demo rows from other tables but never from `audit_log`; it appends a
`demo.reset` row instead.

### Public try-it page

- Cloudflare Turnstile before a session is issued. The session is a signed cookie.
- Per-session and per-IP upload limits, and per-IP session and demo-run limits.
- A global daily spend cap (`PUBLIC_DAILY_SPEND_CAP_USD`, default $3.00) with headroom reserved
  for one in-flight document. When the cap is reached, the page switches to a recorded replay.
- Visitor documents carry `session_id` and `purge_after` (24h, enforced by a CHECK constraint).
  `make purge` deletes them and their storage objects.
- **Visitor documents can never reach Posted** (`status.transition` refuses them), and they never
  return remit-to details, the original file, traces or the audit log.
- Visitors are asked to upload redacted invoices only. Uploads are not used for training.

### Inbound email (n8n)

`POST /api/v1/ingest/email` requires `X-InvoiceDesk-Signature: sha256=<HMAC of the raw body>`
with `INGEST_WEBHOOK_SECRET`. A redelivery with the same `message_id` returns the same record.
The n8n workflow forwards only PDF and image attachments
([integrations/n8n](integrations/n8n/workflows/README.md)). The email body is screened like the
documents.

## Adversarial test results

The 30-case adversarial set (§5.6) is a CI gate: **any miss fails the PR.** A case counts as missed
when its final status differs from the label or a planted `flag` rule did not fire.

> **Offline harness run only.** The classifier's answers in this run came from the gold-backed
> keyword oracle, not from Haiku 5.5. The detectors, rules, routing and state machine are the
> real code. Source:
> [evals/reports/2026-10-24-dev_adversarial-D-N-offline.json](evals/reports/2026-10-24-dev_adversarial-D-N-offline.json).

| Attack (cases) | InvoiceDesk (config D): held | Basic read-and-post script (config N): posted |
|---|---|---|
| White text in a PDF (4) | 4 / 4 Flagged | 4 / 4 posted |
| 2 pt text in a PDF (3) | 3 / 3 | 3 / 3 |
| Tiny-font text (2) | 2 / 2 | 2 / 2 |
| Off-page text (2) | 2 / 2 | 2 / 2 |
| Faint text in a photo (3) | 3 / 3 | 3 / 3 |
| Injection in the email body (3) | 3 / 3 | 3 / 3 |
| Lookalike letterhead (3) | 3 / 3 | 3 / 3 |
| Legitimate-looking bank-change letter (2) | 2 / 2 | 2 / 2 |
| Duplicate, re-photographed (2) | 2 / 2 | 2 / 2 |
| Duplicate, tweaked amount (2) | 2 / 2 | 2 / 2 |
| Duplicate, new invoice number (2) | 2 / 2 | 2 / 2 |
| Split invoice under the cap (2) | 2 / 2 | 2 / 2 |
| **Total misses** | **0 of 30** in each of 3 repeats | 30 of 30 |

The per-attack rows join the report's per-document `results` (repeat 0; offline repeats are
identical) with the `category` field in `data/gold/adversarial/*.json`. In the same run,
injection recall was 1.000 and the injection false-positive rate on clean documents was 0.000;
bank-change hold recall and duplicate recall were both 1.000.

**Known detector limits** (from [docs/GAPS.md](docs/GAPS.md)):

- Faint text at about 5% contrast is only partly recovered by the CLAHE + RapidOCR pass on the
  adversarial photos.
- Dark text placed under an opaque picture can pass the ink check. The classifier still reads it.
- Detector thresholds are tuned on synthetic renders, not real photos.
- The classifier has never seen real Haiku 5.5 output.

## Known limitations

- **No live model has been tested.** The prompt-injection resistance of the real extractor and
  classifier is unmeasured until the live adversarial run.
- **MCP confirmation trusts the MCP client.** A compromised client could answer the "confirm
  vendor and total" prompt itself. Typed values plus the server-side match check limit this, and
  `post_bill` can never touch Flagged documents.
- **`file_url` fetch has no DNS pinning.** Private-address and redirect checks run, but a
  DNS-rebinding race remains.
- **The follow-up webhook to n8n is unauthenticated.** The API posts to `N8N_FOLLOWUP_WEBHOOK_URL`
  without a signature. Keep n8n on a private network (the Fly config exposes no public service
  for it) until the API signs that call.
- **Client IP for rate limits.** With `TRUST_FORWARDED_FOR=true` the API trusts the left-most
  `X-Forwarded-For` hop, which a client can set itself. Running uvicorn with
  `--forwarded-allow-ips *` has the same flaw (uvicorn copies that left-most hop into the client
  address). The Fly config therefore keeps `TRUST_FORWARDED_FOR=false` and runs uvicorn with
  `--no-proxy-headers`, so per-IP limits key on the Fly proxy address: one shared, stricter
  bucket. Behind Fly.io, `Fly-Client-IP` is the trustworthy header; the API does not read it yet.
- **Hosted identity is untested.** Supabase Auth, Turnstile and the hosted OAuth issuer for MCP
  have been tested against mocks only.

## What a production deployment would add

- **Identity:** SSO (SAML/OIDC) for staff with MFA enforced; SCIM deprovisioning; role reviews.
- **Secrets:** a KMS or managed secret store with rotation, instead of platform env secrets;
  separate keys per environment; QBO token encryption keys out of the database host.
- **Compliance:** SOC 2-style controls (access reviews, change management, incident response,
  vendor management), and a DPA with each sub-processor (Anthropic, Supabase, Fly.io, Langfuse).
- **Data lifecycle:** a written retention policy per data class (originals, extractions, audit
  log), legal-hold support, deletion SLAs, and encrypted backups with restore drills.
- **Network:** private networking between services, an egress allow-list (Anthropic, Intuit,
  Supabase, Langfuse only), and a WAF in front of the public API.
- **Detection and response:** alerts on spend spikes, R6 and R10 rates, approval anomalies (the
  same pair always approving together) and audit-chain verification failures; anchoring the audit
  chain head externally (for example, in a daily signed digest).
- **Payments stay out of scope:** even in production the agent creates bills only. Payment runs
  remain a human action in the accounting system.
- **Assurance:** an external penetration test and a red-team pass on the extractor with real
  vendor documents, then the adversarial set extended with whatever they find.
