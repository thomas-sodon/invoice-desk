# InvoiceDesk

**An AI accounts-payable clerk for property managers.** Drop in a pile of messy vendor invoices
(phone photos, scans, PDFs, forwarded emails). InvoiceDesk reads them, checks each one against
your work orders, approved estimates and vendor list, posts the clean ones to QuickBooks, and
sends duplicates, overcharges and fake bank-change requests to a person.

[![ci](https://github.com/thomas-sodon/invoice-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/thomas-sodon/invoice-desk/actions/workflows/ci.yml)
[![eval](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/thomas-sodon/invoice-desk/main/evals/reports/badge.json)](evals/reports/badge.json)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Works with QuickBooks Online (sandbox). All companies, vendors, people and bank details in this
repo are fictional.

## TL;DR

- **The job:** turn a stack of vendor bills into posted QuickBooks bills, catching the duplicate
  plumber bill and the "please update our bank details" scam before anyone pays them.
- **The design:** the AI only *reads*. Plain, tested business rules decide what gets paid, and any
  change to where money goes always needs two people and a phone call.
- **Where the numbers stand:** the pipeline, safety checks and scoring harness are built and tested
  end to end. **No live model benchmark has been run yet**, so this page makes no accuracy, cost
  or speed claims. See [Results](#results).

| Live benchmark | Fraud and duplicate catches (offline run) | Cost per invoice | Speed per invoice |
|---|---|---|---|
| not run yet | 30 of 30 planted attacks held ([report](evals/reports/2026-10-24-dev_adversarial-D-N-offline.md)) | not measured yet | not measured yet |

## The business problem

A 40-unit property manager like our fictional **Maple Row Property Management** gets about 300
vendor invoices a month from plumbers, electricians, landscapers and cleaners. Someone types each
one into QuickBooks. That costs hours every week, and it lets three expensive mistakes through:

1. **Paying the same bill twice.** The plumber emails a PDF and later hands over a paper copy.
2. **Paying more than was approved.** The invoice is $340 over the estimate for Unit 12B, and
   nobody compares them.
3. **Paying a fraudster.** An "updated bank details" note, sometimes hidden in the invoice
   itself, sends the next payment to someone else. Business email compromise drew about
   **$3.05 billion** in reported losses in the FBI's 2025 IC3 report
   ([summary of the IC3 report](https://www.mcdonaldhopkins.com/insights/news/the-sobering-truth-of-the-fbis-2025-internet-crime-complaint-center-report)).

For context only: vendor-published benchmarks that cite Ardent Partners put manual processing at
about $9.90-$12.88 per invoice ([Lido](https://www.lido.app/blog/invoice-processing-cost-benchmarks),
[Parseur](https://parseur.com/blog/ai-invoice-processing-benchmarks)). These are secondhand and come
from vendors, so treat them as a rough range, not a promise.

## What it does

1. **Takes invoices however they arrive.** You can drag and drop a whole batch (JPEG, PNG,
   HEIC, PDF; up to 10 MB and 5 pages each), or forward an email. The email body is checked too.
2. **Reads every field and shows where it found it.** Vendor, invoice number, dates, work order,
   unit, line items, tax and total each get a box on the original page, coloured green, amber or
   red by how sure it is.
3. **Checks the invoice against your books.** Is the vendor approved, or a lookalike? Does the
   work order exist, and is it for this unit? Is the bill within the approved estimate? Is it a
   duplicate? Does the arithmetic add up? Is it over your approval limit? Are the dates sane?
4. **Routes it.** Clean bills go to *Ready to pay*. Anything unclear goes to *Needs review* with a
   one-sentence reason ("$340.00 over approved estimate for Unit 12B (WO-1187)"). Anything risky
   goes to *Flagged*.
5. **Posts clean bills to QuickBooks** with the original attached, and never posts the same bill
   twice, even if you click again.
6. **Drafts the vendor email** ("missing work-order number; please resend") to the address on
   file. A person approves it before it is sent.
7. **Shows the owner dollars and hours:** invoices handled, hands-free share, hours saved, dollars
   caught and cost per invoice.

| A hidden "pay this new account" line, revealed and blocked | Basic AI vs InvoiceDesk on the same batch |
|---|---|
| ![Hidden bank-change instruction revealed in a PDF](docs/media/hidden-text-mock.png) | ![Comparison of a basic read-and-post script with InvoiceDesk](docs/media/compare-mock.png) |

*Screenshots are from the web app's offline demo mode, which replays the synthetic 25-invoice
demo batch. The outcomes shown are scripted from the demo's labels, not measured
([about these images](docs/media/README.md)).*

## Results

> **Benchmark on synthetic data. Not client results.** No live run against Claude has been
> made yet, because the build environment had no API key. The only report so far is an
> **offline harness run**: a stand-in model that returns the gold answers (with some injected
> noise) is driven through the real file processing, safety checks, business rules and routing.
> That tests whether the *checks* catch what they should. It says nothing about how accurately
> Claude reads invoices, what it costs or how fast it is, so none of those numbers are quoted
> here.

What the offline run does show, on 60 development invoices plus the 30-case attack set, 3 repeats
each, with 95% bootstrap intervals. Source:
[evals/reports/2026-10-24-dev_adversarial-D-N-offline.json](evals/reports/2026-10-24-dev_adversarial-D-N-offline.json)
([readable version](evals/reports/2026-10-24-dev_adversarial-D-N-offline.md)).

| Safety check (offline harness run) | InvoiceDesk (config D) | Basic "read it and post it" script (config N) |
|---|---|---|
| Planted attacks missed (in any repeat) | **0 of 30** | 30 of 30 |
| Share of auto-posted bills that should have been held | **0.000** [0.000, 0.000] | 0.592 [0.500, 0.684] |
| Bank-change requests held for a person | **1.000** | 0.000 |
| Duplicates caught | **1.000** | 0.000 |
| Clean invoices posted with no human touch | 0.500 [0.370, 0.643] (target 0.70: **missed**) | 1.000 (it posts everything) |

The attack row is a plain count, so it carries no interval: the report's bootstrap interval for
that count resamples the whole development-plus-attack pool, so it measures how many attack cases
a resample happens to draw, not uncertainty about whether they were caught. The last row is a
real finding. Even when every field is read correctly, the confidence check
holds about half of the clean invoices, because the confidence scorer has not been fitted on
real model output yet. Fixing that needs a live run on the development set
([GAPS.md](docs/GAPS.md), Evals section).

The scoring method, metric definitions and the full experiment plan (Opus-only vs routed, with and
without caching, OCR-only, a commercial baseline and public receipt datasets) are in
[docs/eval-methodology.md](docs/eval-methodology.md). Every report is listed in
[docs/results](docs/results/README.md). The one-page
[AP Automation Report Card](docs/report-card/AP-Automation-Report-Card.pdf) is generated from the
latest run and carries the same offline banner.

## How it stays safe

In plain English:

- **The AI reads; it never decides.** The model that looks at an invoice can only fill in a form.
  It can't post, pay, approve or change anything. Whether a bill is paid is decided by ordinary,
  tested code that applies your rules.
- **Hidden text is checked for before the AI reads anything.** White-on-white text, microscopic
  fonts, text off the edge of the page, and faint grey print in a photo are screened for first.
  A flagged invoice is quarantined and shown to a person with the hidden text boxed in red. The
  checks are not perfect (very faint print is only partly recovered; see
  [known failure modes](#known-failure-modes)), which is why the bank-change rule below does not
  depend on them.
- **Bank changes always go to people.** Any new bank or remit-to detail, whether from the invoice,
  the email or hidden text, needs two different approvers, neither of them the person who
  uploaded it, plus a confirmed call-back to the vendor's number on file. No setting turns this
  off.
- **The QuickBooks connection can only create bills.** It cannot make payments, edit vendors or
  touch bank details, and every bill carries a unique key so a retry never double-posts.
- **Everything is on the record.** Every step, check result and human action goes into a
  tamper-evident log, where each entry is chained to the one before it.
- **Account numbers are masked** to the last 4 digits everywhere: screens, logs and traces.
- **The public "try it" page never posts anything,** deletes uploads after 24 hours, and has a
  daily spending cap.

The full threat model and controls are in [SECURITY.md](SECURITY.md).

## How it adapts to your business

- **Your vendor list, work orders, estimates and properties** load into the same tables the demo
  uses (`make seed` shows the shape; see `data/seed/`).
- **Your rules live in one file:** [`tenants/maple_row/rules.yaml`](tenants/maple_row/rules.yaml).
  Approval limit, estimate tolerance, duplicate window, date limits, required fields and
  confidence thresholds are all set there, in cents and percentages. Each client gets their own
  file.
- **Your accounting system:** QuickBooks Online is built. Xero is the planned second connector,
  and it plugs into the same two-operation interface.
- **Your inbox:** point the email workflow at your AP mailbox
  ([integrations/n8n](integrations/n8n/workflows/README.md)).
- **Your numbers:** the first step of a pilot is running about 100 of your own redacted
  invoices through the scoring harness, so you see accuracy on *your* documents before anything
  goes live.

## What a production deployment would add

Single sign-on for staff; secrets in a managed key store; a written data-retention policy and a
data processing agreement with each provider; SOC 2-style access reviews and change control;
production QuickBooks (or Xero) with per-client expense-account mapping; monitoring and alerting
on queue depth, error rate and daily spend; backups and a restore drill; and a pilot on real
invoices to replace the synthetic benchmark. [SECURITY.md](SECURITY.md#what-a-production-deployment-would-add)
has the security side in detail.

---

## Technical appendix

### Quickstart (offline: no keys, no spend)

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22 with pnpm, and Docker for
the full stack. Offline mode (`INVOICEDESK_MODE=offline`, the default) never calls a paid or
external API. Claude is replaced by a gold-backed oracle, QuickBooks by an idempotent fake, and
storage is local.

```bash
make install                 # uv sync + pnpm install
make test                    # pytest (throwaway Postgres clusters) + web typecheck/lint/tests
make test-e2e                # the 25-invoice demo + bank-change e-mail end to end, offline
make lint typecheck

# UI only, in the browser's mock mode (no backend needed):
pnpm --filter web dev        # http://localhost:3000

# Full stack (Postgres, api, worker, mcp, n8n, Mailpit):
cp .env.example .env
make up
make seed                    # Maple Row universe, fixed Faker seed
NEXT_PUBLIC_API_BASE=http://localhost:8000 pnpm --filter web dev

make eval-offline            # $0 harness run + Report Card PDF
make audit-verify            # recompute the audit-log hash chain
```

Going live means setting `INVOICEDESK_MODE=live` plus the keys in `.env.example`. Live eval runs
cost real money and refuse to start without `--i-know-this-costs-money`
([evals/README.md](evals/README.md)). Run `make help` for every target.

### Architecture

```text
upload / email (n8n) / agent ──> FastAPI gateway ──> Postgres job queue ──> LangGraph worker
   preprocess (render, 1,600 px, OCR words) ─> screen (hidden text, faint text, Haiku classifier)
   ─> tiered extraction (Haiku -> Sonnet -> Opus, no tools) ─> calibrate ─> rules R1-R10
   ─> Ready to pay | Needs review | Flagged ─> human review ─> QuickBooks Online sandbox
```

Diagrams with the real module names and the full state machine are in
[docs/architecture.md](docs/architecture.md). The decisions behind them:

- [ADR-0001: scope and cut line](docs/adr/0001-scope.md)
- [ADR-0002: LangGraph vs a plain Postgres state machine](docs/adr/0002-langgraph.md)
- [ADR-0003: Claude model routing and API specifics](docs/adr/0003-claude-routing.md)
- [ADR-0004: OCR engine, word-ID anchoring, 1,600 px pages](docs/adr/0004-ocr-anchoring.md)
- [ADR-0005: guardrails as architecture](docs/adr/0005-guardrails.md)
- [ADR-0006: crash-safe pipeline steps and hold-scoped release](docs/adr/0006-pipeline-state-and-release.md)
- [ADR-0007: demo numbers, calibration and board headlines](docs/adr/0007-demo-calibration-and-board.md)
- [ADR-0008: a held invoice is fixed before the two-person release](docs/adr/0008-release-needs-verified-values.md)

Stack: Python 3.12 (FastAPI, LangGraph, Pydantic v2, psycopg, PyMuPDF, RapidOCR), the official
`anthropic` SDK with structured outputs, Next.js 15 + TypeScript, Postgres (Supabase-compatible
migrations), n8n, OpenTelemetry GenAI spans to Langfuse, and GitHub Actions.

### Known failure modes

These come from the offline report and the measured gaps in [docs/GAPS.md](docs/GAPS.md):

1. **Over-cautious confidence gate.** In the offline run, all 72 of config D's failed results were
   labelled calibration misses by the heuristic root-cause labeller: R9 held a correctly read
   clean invoice (rule R9 precision 0.049,
   [report](evals/reports/2026-10-24-dev_adversarial-D-N-offline.md)). The calibrator must be fitted
   on live dev runs.
2. **OCR token merges break word anchoring.** RapidOCR sometimes merges words on scans
   (`"WorkorderWO-1180"`), and some PDF text layers come out fragmented. Fields lose their word
   references, confidence drops, and the 25-invoice demo settles at 10 / 12 / 3 instead of the
   designed 18 / 4 / 3 when run through the full pipeline (GAPS.md, API section).
3. **Rule false positives in the offline run.** R2, R3, R5 and R7 fire on some documents whose
   labels don't plant them (precision 0.58, 0.75, 0.40 and 0.40). Part of this is a known label
   gap (`core-117`, GAPS.md).
4. **Image-hash duplicates are unreliable.** Distinct invoices from the same vendor template can
   be within the pHash threshold, and true re-photographed duplicates are not. Those are caught by
   vendor and invoice number instead (GAPS.md, data section).
5. **Detector limits.** Faint text at about 5% contrast is only partly recovered, and dark text
   under an opaque picture can pass the ink check. The classifier still reads that text, but it
   is a weaker layer (GAPS.md, Guardrail screening section).

### Agent access

A small remote MCP server (`services/mcp-server`, 2026-07-28 spec, stateless, OAuth Protected
Resource Metadata) exposes `extract_invoice`, `validate_invoice` and `post_bill`. `post_bill`
refuses anything that is not *Ready to pay*, always refuses *Flagged*, and asks the human to
confirm the vendor and total before posting. See [SECURITY.md](SECURITY.md) and
`services/mcp-server/`.

### Repo layout

`apps/web` (Next.js) · `apps/api` (FastAPI) · `packages/core/invoicedesk` (the shared library:
preprocess, guardrails, extract, calibrate, rules, pipeline, accounting, audit, telemetry) ·
`services/worker` · `services/mcp-server` · `evals` · `data` · `tenants` ·
`integrations/n8n` · `infra` ([Fly.io configs](infra/fly/README.md)) · `docs`. The full map is in
[docs/foundation.md](docs/foundation.md). Open gaps are tracked in [docs/GAPS.md](docs/GAPS.md).

### License

[MIT](LICENSE). QuickBooks is a trademark of Intuit Inc. This project is not affiliated with or
endorsed by Intuit.
