# CLAUDE.md: InvoiceDesk

InvoiceDesk is an AI accounts-payable clerk and the flagship piece of an Upwork AI-developer portfolio. It reads messy vendor invoices (PDFs, scans, phone photos, forwarded emails) and checks each one against work orders, approved estimates and the vendor list. Clean bills are posted to a QuickBooks Online sandbox. Duplicates, overcharges and bank-change fraud are routed to a human.

**The spec is `DESIGN-InvoiceDesk.md`. Read the relevant section before building a component.** Most-used sections:

| Need | Section |
|---|---|
| MVP features (M1–M21) and the cut line | §3 |
| Components, data flow, document state machine | §4.1–4.3 |
| Model routing, API specifics | §5.1–5.2 |
| Rules R1–R10, guardrails, MCP tools | §5.5 |
| Metrics, targets, experiment matrix, CI gates | §5.6 |
| Postgres schema, synthetic data | §6 |
| Day-by-day plan | §8 |
| Repo layout | §12 |

If code and the design doc disagree, raise it. Don't silently diverge. Record real decisions as ADRs in `docs/adr/`.

## Status

The MVP components are built and wired, following the layout in §12: `apps/web` (Next.js), `apps/api` (FastAPI), `packages/core` (the Python library everything imports), `services/worker`, `services/mcp-server`, `data/`, `evals/`, `tenants/`, `infra/`, plus `tests/e2e` (offline end-to-end demo). Nothing has run against a live model, QBO sandbox or Supabase yet, and nothing is deployed: `docs/GAPS.md` lists every gap. Add new targets in `mk/<component>.mk` and keep the list below true.

## Commands (keep this list true)

Every target below exists (`make help` lists them; components add theirs in `mk/<component>.mk`).
Offline mode (`INVOICEDESK_MODE=offline`, the default) needs no keys and calls no paid API.

```bash
make install            # uv sync + pnpm install
make lint               # ruff check + ruff format --check + web eslint
make typecheck          # pyright + web tsc
make test               # pytest (incl. tests/e2e, Postgres via throwaway cluster) + web typecheck/lint/vitest
make cov                # pytest with branch coverage for packages/core
make up / down / logs   # docker compose: Postgres, api, worker, mcp, n8n, Mailpit
make migrate            # apply infra/supabase/migrations to $DATABASE_URL
make audit-verify       # recompute the audit-log hash chain
make seed               # seed business universe (fixed Faker seed); seed-csv writes CSVs only
make api / worker / worker-once / mcp   # run a service locally (offline fakes unless live)
make mcp-devtoken       # offline only: dev JWKS + bearer token for the MCP server
make purge              # delete try-it uploads older than 24h (audit rows stay)
make demo-reset         # delete sandbox demo bills, re-seed the board (dry run; APPLY=1 to apply)
make qbo-bootstrap      # one-time QBO sandbox OAuth (prints QBO_REALM_ID / QBO_REFRESH_TOKEN)
make validate EX=extraction.json TD=tenant_data.json   # run R1-R10 on JSON
make eval CONFIG=D      # run eval config (A B C D Dp E F G N; Dp = D'); costs real money when live
make eval-offline       # $0 D + N on dev+adversarial with the oracle (n=3) + Report Card
make eval-smoke         # $0 offline smoke run + the PR gate vs evals/baselines/smoke-offline.json
make eval-fit-thresholds / eval-estimate   # tau on DEV only; live cost estimate
make report-card        # render AP Automation Report Card PDF from latest eval run
make data-all           # (re)build seed CSVs, synthetic-core, adversarial set, demo batch
make data-generate / data-adversarial / data-demo / data-splits / data-verify / data-eval-examples / data-public
make web-dev / web-build / web-fixtures / web-screenshots
make test-e2e           # offline end-to-end demo (board 18/4/3, R6 holds, idempotent posting)
make test-rules / test-pipeline / test-api / test-mcp / test-accounting / test-data / test-evals / test-web
```

Repo layout, shared contracts, entry points and migration ranges: `docs/foundation.md`.

Tooling defaults: Python 3.12 with `uv`, `ruff` (lint and format), `pyright`, `pytest`; TypeScript strict with `pnpm`. Run lint, typecheck and the relevant unit tests before every commit.

## Non-negotiable invariants

These are the product's core claims. Never weaken them, not even temporarily or "for the demo".

1. **The LLM reads; code decides.** Routing, holds and posting are deterministic Python. No LLM output can move a document to `ReadyToPay` or `Posted` on its own authority.
2. **The extractor has no tools.** It is the model that sees untrusted document text and images, and it only returns schema-validated JSON (dual-LLM / quarantined-reader pattern). Components with tools (the posting adapter, the MCP server) never receive raw document text.
3. **Bank or remit-to changes (R6) always go to a human.** That means two-person approval plus a callback attestation, and the approver must not be the uploader. No code path, MCP tool or config flag lets the agent clear R6.
4. **The posting adapter is least-privilege.** It can only create a Bill and attach a file: no BillPayment, no vendor edits, no bank fields. Every post carries an idempotency key (`requestid`), so a retry can never double-post.
5. **`audit_log` is append-only and hash-chained.** Never UPDATE or DELETE its rows. Every state change, detector verdict, rule result and human action writes a row.
6. **Money is integer cents.** Never use floats for amounts. Dates are ISO 8601.
7. **Rules are pure functions** of (extraction, tenant data, config), with config in `tenants/<tenant>/rules.yaml`. `packages/core/rules/` requires 100% branch coverage.
8. **Screening comes before extraction.** A document flagged by the hidden-text or injection detectors is quarantined. It is never escalated to higher tiers with its hidden text unflagged.
9. **Mask bank and account numbers to the last 4** in logs, traces and the UI, everywhere.

## Claude API rules (checked 2026-10-24)

Use the official `anthropic` Python SDK. Model IDs live in one config module; never hardcode them at call sites.

| Role | Model | Settings |
|---|---|---|
| Injection classifier, tier-1 extraction, email/variance wording | `claude-haiku-5-5` | `output_config.effort: "low"` |
| Tier-2 escalation | `claude-sonnet-5-5` | `effort: "low"`, adaptive thinking |
| Tier-3 adjudication | `claude-opus-5-5` | `effort: "medium"`, set explicitly |
| Offline eval root-cause labeling | `claude-opus-5-5` | `effort: "high"`, Batch API |

- **No sampling parameters.** `temperature`, `top_p` and `top_k` return a 400. Outputs are non-deterministic, so evals run n=3 with bootstrap CIs.
- **Structured output via `client.messages.parse()`** with Pydantic models (`output_config.format`). Don't use forced `tool_choice` (`any`/`tool`): it returns a 400 on Opus 5.5 and Sonnet 5.5. Don't use assistant prefill: it is a 400 too.
- **Thinking:**
  - Opus 5.5 can't disable thinking, so control it with effort; its default effort is `medium`.
  - Sonnet 5.5 returns a 400 on `{type: "disabled"}`; use `{type: "between_tools"}` if you truly need thinking off.
  - Haiku 5.5 accepts `disabled` only at effort `high` or below.
  - Always set effort explicitly.
- **Refusals.** Check `stop_reason == "refusal"` before reading `content`.
  - Haiku 5.5 has no server-side fallback, so a refusal escalates to Sonnet 5.5.
  - Sonnet and Opus requests carry `fallbacks: "default"` with beta `server-side-fallback-2026-07-01`.
- **Files API is GA** (`client.files.upload`, no beta header). Upload each page once and reuse the `file_id` across tiers.
- **Prompt caching.**
  - Caches are per model, and the minimum cacheable prefix is 512 tokens.
  - Each tier keeps a frozen prefix (system prompt, schema description, few-shot examples). Put volatile content after the breakpoint.
  - Verify caching with `usage.cache_read_input_tokens`.
  - Never put timestamps or IDs in the system prompt.
- **Images.** Pre-resize pages to a 1,600 px long edge so box coordinates map exactly; the API maximum is 2,576 px. Anchor boxes to OCR word IDs and use model-drawn boxes only as a fallback for handwriting.
- **Errors.** Catch specific SDK exception classes (rate limit vs status vs connection) rather than one broad class. Use per-tier semaphores; worker concurrency is 8.
- **Record cost and latency on every call** from `usage` × the price table in the `extractions` table, and emit OpenTelemetry GenAI spans to Langfuse.

The API drifts. When something here looks wrong, check current docs, then update this section.

## Evals

- **Never look at the test split.** `synthetic-core` is 60 dev / 60 test, frozen. Tune thresholds (`tau_field`, `tau_hi`), prompts and the calibrator on dev only, and never edit test labels.
- The demo seed batch (`data/demo/`) is excluded from all eval splits.
- Any change to prompts, extraction, calibration, routing or rules needs a before/after eval on the smoke set, with results reported.
- **CI gates on PRs**:
  - field F1 must not drop more than 1.0 pt vs `main`;
  - false-STP ≤ 1%;
  - zero misses on the 30-case adversarial set;
  - $/doc must not rise more than 20%.
- **Eval runs cost real money.** The PR smoke run is about $0.70. A full matrix run is about $40, and you must ask before running it.
- Never redistribute SROIE or CORD images. Ship only the download scripts and field mappings.

## Data and safety

- All data is synthetic and fictional: Maple Row Property Management, its vendors and its properties. Never add real company names, real people or real bank details. Bank numbers use reserved test formats.
- The public try-it page:
  - never posts visitor uploads to accounting;
  - auto-deletes uploads after 24h;
  - enforces Turnstile, per-IP and per-session limits, and a global $3/day spend cap that falls back to a recorded replay.
- Secrets live only in `.env` (never commit it); keep `.env.example` current. The QBO sandbox only; never connect a production realm.
- Use "works with QuickBooks Online" wording only, with no Intuit or Xero logos.

## Scope discipline

- The MVP is M1–M21 in §3. Stretch items (Xero, Slack approvals, attachment classification, and others) wait until after the week-4 deploy. If the schedule slips, cut using §3's cut line; don't add features.
- Keep MCP and OWASP jargon off the landing page and owner dashboard: they speak dollars and hours. Technical depth goes in the README appendix, `SECURITY.md` and `docs/`.
- Portfolio and README copy must not over-claim. Fill every metric from the latest `eval_runs` row, label it "benchmark on synthetic data", and make no client claims until real pilots exist.
