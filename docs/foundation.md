# Foundation: layout, shared contracts and conventions

Read this before you build a component. It records how the repo maps onto DESIGN §12, which
contracts are shared (and so frozen for component work), and where each component plugs in.

## Layout (DESIGN §12, with Python packaging made explicit)

The repo is a uv workspace (root `pyproject.toml`, `uv.lock`) and a pnpm workspace (`apps/web`).
Every Python member is an installable package, so the importable module directories carry a
package name. That is the only difference from the §12 tree:

| §12 path | Actual module path | Import as |
|---|---|---|
| `packages/core/<x>/` | `packages/core/invoicedesk/<x>/` | `invoicedesk.<x>` |
| `apps/api/` | `apps/api/invoicedesk_api/` | `invoicedesk_api` |
| `services/worker/` | `services/worker/invoicedesk_worker/` | `invoicedesk_worker` |
| `services/mcp-server/` | `services/mcp-server/invoicedesk_mcp/` | `invoicedesk_mcp` |
| `evals/runner.py`, `evals/metrics/` | `evals/invoicedesk_evals/{runner.py,metrics/}` | `invoicedesk_evals` |
| `data/generator/` etc. (code) | `data/invoicedesk_data/{generator,...}` | `invoicedesk_data` |
| `data/seed`, `data/gold`, `data/demo`, `data/public` (artifacts) | unchanged | n/a |

So "`packages/core/rules/` requires 100% branch coverage" applies to
`packages/core/invoicedesk/rules/`, measured by tests in `packages/core/tests/rules/`.

Tests live in `<member>/tests/` (core: `packages/core/tests/<component>/`). Run everything with
`make test`. Pytest uses `--import-mode=importlib`, so test directories need no `__init__.py`. Give
test files unique basenames anyway.

## Shared contracts (frozen; change only via needs_outside_ownership)

| Module | What it fixes |
|---|---|
| `invoicedesk.money` | Integer-cents helpers, the `Cents` pydantic type (strict int), bps tolerance math (`within_tolerance` is R3's `max(10%, $100)` shape) |
| `invoicedesk.masking` | `mask_number`, `mask_text`, `mask_payload` (last 4). Use them for logs, traces, UI and the audit log. |
| `invoicedesk.remit` | `remit_fingerprint(text)`, the salted hash stored in `vendors.remit_to_hash` and compared by R6 |
| `invoicedesk.status` | `DocStatus`, `TRANSITIONS` (exactly §4.3), `transition()` with evidence gates, `route()`, `Actor`, `kanban_column` |
| `invoicedesk.contracts.extraction` | `InvoiceExtraction` (§5.3 verbatim, `extra="forbid"`), `HEADER_FIELDS` (13), `REQUIRED_FIELDS`, `field_value()` |
| `invoicedesk.contracts.llm` | `LLMClient` Protocol (`upload_file`, `parse`), `LLMRequest` (role, frozen system blocks with cache flags, user blocks), `LLMResponse`, `Usage`, `LLMCallError` kinds |
| `invoicedesk.contracts.preprocess` | `PreprocessedDocument`, `OcrWord`, `TextLayerSpan`, `PageImage`, `Preprocessor`, `OcrEngine`, limits (10 MB, 5 pages, 1,600 px) |
| `invoicedesk.contracts.screening` | `ScreenResult`, `ScreenSpan`, `InjectionVerdict` (classifier output), `ScreeningOutcome`, `should_quarantine()` (the R10 policy) |
| `invoicedesk.contracts.calibration` | `FieldSignals`, `Calibrator` |
| `invoicedesk.contracts.extractor` | `Extractor`, `ExtractionOutcome`, `TierRun` (= an `extractions` row), `FieldResult` (= a `fields` row) |
| `invoicedesk.contracts.tenant_data` | `TenantData`, `Vendor`, `WorkOrder`, `EstimateLine`, `PriorInvoice`, `ApprovalRecord` |
| `invoicedesk.contracts.rules` | `RuleContext` (pure input), `RuleResult`, `Rule`, `RulesEngine`, `DocumentInfo` |
| `invoicedesk.contracts.accounting` | `AccountingAdapter` (only `create_bill` and `attach_file`), `BillRequest` (`extra="forbid"`, no bank fields), `idempotency_key_for()` |
| `invoicedesk.contracts.infra` | `Storage`, `EventPublisher` + `BoardEvent`, `JobQueue` + `Job` |
| `invoicedesk.config.models` | **The only place model IDs live.** `ModelRole`, `ROLE_SETTINGS` (effort, thinking, fallbacks, betas), `PRICES`, `cost_usd()` |
| `invoicedesk.config.tenant` | `RulesConfig` schema for `tenants/<t>/rules.yaml` (cents and bps, `extra="forbid"`) |
| `invoicedesk.config.settings` | `Settings` from `.env` (`INVOICEDESK_MODE=offline|live`) |
| `invoicedesk.db` | `Database` (psycopg pool, `tx()`), `apply_migrations`, `repo.*` (every write also appends its audit rows), `queue.PostgresJobQueue` |
| `invoicedesk.audit` | `append()` / `verify()`, the hash chain. The DB trigger forbids UPDATE, DELETE and TRUNCATE. |
| `invoicedesk.fakes` | `FakeLLM`, `ReplayLLM`, `RecordingLLM`, `FakeAccounting`, `InMemoryEventPublisher`, `InMemoryJobQueue` |
| `invoicedesk.storage` | `LocalStorage` (HMAC-signed URLs served by the API at `/files/{key}`), `InMemoryStorage` |
| `invoicedesk.wiring` | Composition root: offline uses fakes and replay; live uses the real modules named below |
| `invoicedesk.testing` | pytest plugin with `pg_server`, `pg_dsn` and `pg_db` (a throwaway cluster, or `INVOICEDESK_TEST_DSN`), and `builders.make_extraction()` |
| `docs/api-contract.md` | REST, realtime and agent-facade contract between api, web, mcp and n8n |

## Entry points components must provide

These are the names `invoicedesk.wiring`, the CLI and other components import. Stubs that raise
`NotImplementedError` exist until the owner replaces them.

| Owner | Must provide |
|---|---|
| preprocess | `invoicedesk.preprocess.build_preprocessor(storage, *, ocr=None) -> Preprocessor`; `invoicedesk.preprocess.cli:main` |
| guardrails | `invoicedesk.guardrails.build_screeners(llm) -> list[Screener]`; `run_screening(doc, screeners, *, email_body) -> ScreeningOutcome`; `invoicedesk.guardrails.cli:main` |
| extraction | `invoicedesk.llm.anthropic_client.AnthropicLLMClient(api_key=...)` (the only module that may import `anthropic`); `invoicedesk.extract.build_extractor(llm, *, calibrator, storage) -> Extractor`; `invoicedesk.calibrate.load_calibrator(path=None) -> Calibrator`; `invoicedesk.extract.wording.polish_variance(llm, sentence) -> str` and `draft_followup(llm, *, vendor, issues) -> FollowupDraft`; `invoicedesk.telemetry.{setup_tracing, llm_span}`; `invoicedesk.extract.cli:main` |
| rules | `invoicedesk.rules.build_rules_engine() -> RulesEngine`; `invoicedesk.rules.cli:main` |
| accounting | `invoicedesk.accounting.build_accounting_adapter(settings) -> AccountingAdapter` (QBO sandbox) |
| pipeline | `invoicedesk.pipeline.events:PgNotifyPublisher.from_settings(s)`; `invoicedesk.pipeline.services` (process, apply_corrections, record_approval, post_document, comparison); `invoicedesk.pipeline.naive` (config N); worker `invoicedesk-worker` |
| data | `invoicedesk_data.seed.seed_database(db, tenant_id="maple_row")`; `invoicedesk_data.demo.demo_files() -> list[DemoFile]`; `invoicedesk_data.oracle.OracleLLM` (a deterministic gold-backed `LLMClient` for offline demo and replay); gold loaders for evals |
| api | `invoicedesk_api.main:app`; `invoicedesk_api.storage_supabase:SupabaseStorage.from_settings(s)` |
| mcp | `invoicedesk_mcp.server:main` |

## Decisions and interpretations of the spec (raise if you disagree)

1. **Release from NeedsReview.** §4.3 has no NeedsReview -> ReadyToPay edge. The only way out
   is NeedsReview -> Validating (after a human action) -> re-route. Over-cap (R7) and new-vendor
   (R1) holds clear when `RuleContext.approvals` holds an `approve` of kind `over_cap` or
   `new_vendor` from someone other than the uploader. The rules stay pure: approvals are input data.
2. **Release from Flagged** (any cause: bank change, duplicate, injection, lookalike) always needs
   `ApprovalEvidence` with two distinct approvers, neither of them the uploader, and at least one
   callback attestation. For `bank_change`, the DB also requires `callback_attested` on every
   approve row. No system or MCP actor can produce this transition.
3. **ReadyToPay** from Validating needs a `RoutingDecision` that `transition()` recomputes from
   all ten rule outcomes, so a forged or partial decision is rejected. LLM actors can't transition.
4. **Visitor (try-it) documents** have `session_id` set (and `purge_after`, enforced by a CHECK)
   and can never reach Posted.
5. **Resolved vendor.** After R1 resolves a vendor, the pipeline stores a `fields` row named
   `vendor_id` (final) so later documents' R4 duplicate checks can key on it. `load_tenant_data`
   reads that row.
6. **Costs** are `numeric(12,8)` USD in the DB, `Decimal` in Python and decimal strings in JSON.
   Invoice money is always `bigint` cents.
7. **Audit actions** use dotted names: `document.created`, `status.changed`, `screen.<detector>`,
   `rule.R<n>`, `review.field_corrected`, `approval.approve|reject`, `bill.pending|posted|failed`.
   Add new ones in the same style.
8. **OCR default is RapidOCR** (onnxruntime, models bundled, line boxes split into word boxes by
   the preprocessor). docTR is an optional extra (`uv sync --extra doctr`) behind the same
   `OcrEngine` Protocol (see docs/GAPS.md).
9. **Offline mode** is the default. No component may call a paid or external API unless
   `INVOICEDESK_MODE=live`. Tests never do; mark any live test `@pytest.mark.live`.

## Migrations

`infra/supabase/migrations/*.sql` are applied in filename order by `invoicedesk migrate` (and by
the test fixtures). `0001_init.sql` implements §6.1; additions are marked `ADDITION`. Components
that need schema changes add new files in their own number range and never edit others' files:

| Range | Owner |
|---|---|
| 0001-0099 | foundation |
| 0100-0199 | rules |
| 0200-0299 | data |
| 0300-0399 | pipeline (LangGraph's `PostgresSaver.setup()` creates its own checkpoint tables) |
| 0400-0499 | api |
| 0500-0599 | accounting |
| 0600-0699 | evals |
| 0700-0799 | mcp |

## Make targets

The root `Makefile` holds the shared targets (`install lint format typecheck test cov up down
logs migrate audit-verify`) and includes `mk/*.mk`. Components add their own targets in
`mk/<component>.mk`, e.g. `seed`, `eval`, `demo-reset` and `report-card`. Only add targets that
work, and update the command list in CLAUDE.md when you do.
