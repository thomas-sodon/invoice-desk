# ADR-0001: Scope of the MVP

- **Status:** accepted
- **Date:** 2026-10-24
- **Spec:** [DESIGN §3](../../DESIGN-InvoiceDesk.md#3-user-stories-mvp-feature-list-and-stretch-features), §8 (build plan), §11 (risks)

## Context

InvoiceDesk is a portfolio piece built by one developer in four weeks. It has to show a complete
back-office workflow: read invoices, check them against business data, route exceptions to a
person, and post clean bills to accounting. It also has to show the engineering evidence that
technical buyers look for. Both scope creep and an unfinished thesis are real risks (§11 rates
scope creep High/High).

## Decision

1. **The MVP is M1-M21 in §3, and nothing else.** Stretch items S1-S7 (Xero, Slack approvals,
   attachment classification, MCP Apps UI, distillation, property-management adapters, Batch
   overnight mode) wait until after the week-4 deploy.
2. **Cut line, in this order:** S-items first, then M15's *send* step (keep the follow-up draft),
   then CORD (keep SROIE). **M7, M8, M11, M13, M17 and M19 are never cut.** They are the thesis:
   deterministic rules, guardrails, real posting, the naive comparison, evals and CI gates.
3. **One fictional tenant.** Maple Row Property Management, its 30 vendors and its 6 properties are
   invented. No real company names, people or bank details, and bank numbers use reserved test
   formats.
4. **Sandbox-only accounting.** The QuickBooks Online adapter refuses any environment other than
   `sandbox`. Wording is "works with QuickBooks Online", with no logos.
5. **Offline-first.** Every external dependency (Claude, QBO, Supabase, Langfuse, Turnstile, n8n)
   sits behind an interface with a deterministic fake or replay. Offline mode is the default, so
   the repo runs, tests and demos with no keys and no spend. See
   [docs/foundation.md](../foundation.md).
6. **Honesty over polish.** Every published number comes from an eval report file and is labelled
   "benchmark on synthetic data". When an environment cannot do something the spec asks for (real
   photos, a live model, a deploy), the gap goes in [docs/GAPS.md](../GAPS.md), not into the
   copy.

## What was built against this scope

| Feature | Where | State |
|---|---|---|
| M1 batch ingest, M14 try-it | `apps/api/invoicedesk_api/{uploads,routes/tryit}.py`, `apps/web` | built, offline-tested |
| M2 email-thread ingest via n8n | `apps/api/.../routes/ingest.py`, [integrations/n8n](../../integrations/n8n/workflows/README.md) | built; n8n itself not run here |
| M3 preprocessing | `packages/core/invoicedesk/preprocess/` | built (RapidOCR default, see [ADR-0004](0004-ocr-anchoring.md)) |
| M4 tiered extraction, M6 calibration | `invoicedesk/extract/`, `invoicedesk/calibrate/` | built; no live Claude call yet |
| M7 rules R1-R10 | `invoicedesk/rules/` | built, 100% branch coverage gate |
| M8 guardrails | `invoicedesk/guardrails/`, `invoicedesk/audit/` | built ([ADR-0005](0005-guardrails.md)) |
| M9-M10 board, review | `apps/web`, `invoicedesk/pipeline/` | built |
| M11 QBO posting | `invoicedesk/accounting/` | built; tested against a fake QBO server |
| M12-M13 dashboard, comparison | `apps/api/.../routes/owner.py`, `invoicedesk/pipeline/naive.py` | built |
| M15 vendor follow-up | `invoicedesk/extract/wording.py`, [vendor-followup.json](../../integrations/n8n/workflows/vendor-followup.json) | built; Mailpit send not run here |
| M16 MCP server | `services/mcp-server/` | built |
| M17-M19, M21 evals, CI, Report Card | `evals/`, `.github/workflows/` | built; offline runs only |
| M20 observability | `invoicedesk/telemetry/` | built; Langfuse not connected |

## Consequences

- The README and Upwork copy can only cite what the eval reports contain. Until a live run exists,
  that means harness and pipeline evidence, not model accuracy.
- New feature requests during the build go into the stretch list, not the sprint.
- Physical tasks the spec lists (photographing printed invoices, hand-filling carbon books,
  recording the video) are simulated or deferred, and each one is listed in GAPS.md.
