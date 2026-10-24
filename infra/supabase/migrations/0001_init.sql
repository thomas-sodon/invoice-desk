-- InvoiceDesk schema (DESIGN §6.1). Plain Postgres 15+; runs on Supabase or a local cluster.
-- Money is integer cents (bigint). API cost in USD is numeric. Timestamps are timestamptz.
-- Additions beyond §6.1 are marked "ADDITION" and explained in docs/foundation.md.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- enums
create type document_status as enum (
  'Received','Screening','Quarantined','Extracting','Extracted','Validating',
  'ReadyToPay','NeedsReview','Flagged','Rejected','Posted');
create type document_source as enum ('upload','email','seed','mcp');
create type vendor_status as enum ('approved','pending','blocked');
create type wo_status as enum ('open','closed');
create type llm_tier as enum ('haiku','sonnet','opus');
create type approval_kind as enum ('bank_change','over_cap','new_vendor','duplicate','injection','other');
create type approval_decision as enum ('approve','reject');
create type eval_split as enum ('dev','test','adversarial','public','correction','smoke','demo');
create type spend_scope as enum ('global','ip','session');
create type job_status as enum ('queued','running','done','failed');
create type staff_role as enum ('clerk','approver','owner');

-- ---------------------------------------------------------------- business universe
create table tenants (
  id          text primary key,                 -- slug, e.g. 'maple_row'
  name        text not null,
  rules_yaml  text not null default ''
);

-- ADDITION: staff users (ids match Supabase Auth user ids in production).
create table staff_users (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    text not null references tenants(id),
  email        text not null unique,
  display_name text not null,
  role         staff_role not null default 'clerk',
  created_at   timestamptz not null default now()
);

create table properties (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  text not null references tenants(id),
  name       text not null,
  address    text not null
);

create table units (
  id           uuid primary key default gen_random_uuid(),
  property_id  uuid not null references properties(id),
  label        text not null,
  unique (property_id, label)
);

create table vendors (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      text not null references tenants(id),
  legal_name     text not null,
  aliases        text[] not null default '{}',
  address        text not null default '',
  tax_id_last4   text check (tax_id_last4 ~ '^[0-9]{4}$'),
  remit_to_hash  text,
  bank_last4     text check (bank_last4 ~ '^[0-9]{4}$'),
  phone_on_file  text,
  email          text,                         -- ADDITION: follow-up emails go to the vendor master contact
  qbo_vendor_ref text,                         -- ADDITION: external vendor id for create_bill
  status         vendor_status not null default 'approved',
  unique (tenant_id, legal_name)
);

create table work_orders (
  id                      uuid primary key default gen_random_uuid(),
  tenant_id               text not null references tenants(id),
  wo_number               text not null,
  unit_id                 uuid not null references units(id),
  vendor_id               uuid not null references vendors(id),
  scope                   text not null,
  status                  wo_status not null default 'open',
  approved_estimate_cents bigint not null check (approved_estimate_cents >= 0),
  approved_by             text,
  approved_at             timestamptz,
  unique (tenant_id, wo_number)
);

create table estimate_lines (
  id             uuid primary key default gen_random_uuid(),
  work_order_id  uuid not null references work_orders(id) on delete cascade,
  description    text not null,
  amount_cents   bigint not null
);

-- ADDITION: 18 months of historical bills for duplicate checks (§6.2 seed).
create table historical_bills (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       text not null references tenants(id),
  vendor_id       uuid not null references vendors(id),
  invoice_number  text not null,
  invoice_date    date not null,
  total_cents     bigint not null,
  phash           text
);
create index on historical_bills (tenant_id, vendor_id, invoice_number);

-- ---------------------------------------------------------------- documents
-- ADDITION: batches group documents for the board's live cost meter and comparison mode.
create table batches (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   text not null references tenants(id),
  label       text not null default '',
  mode        text not null default 'live' check (mode in ('live','demo','replay','tryit')),
  session_id  text,
  created_by  uuid references staff_users(id),
  created_at  timestamptz not null default now()
);

create table documents (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     text not null references tenants(id),
  batch_id      uuid references batches(id),                 -- ADDITION
  source        document_source not null,
  session_id    text,             -- anonymous try-it session; non-null => never posted
  uploaded_by   uuid references staff_users(id),             -- ADDITION: approver != uploader
  filename      text,                                        -- ADDITION
  sha256        text not null,
  phash         text,
  storage_path  text not null,
  pages         int not null default 1 check (pages between 1 and 5),
  mime          text not null,
  status        document_status not null default 'Received',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),          -- ADDITION
  purge_after   timestamptz,
  total_cost_usd numeric(12,8) not null default 0,           -- ADDITION: sum of extraction+screen cost
  check (session_id is null or purge_after is not null)
);
create index on documents (tenant_id, status);
create index on documents (batch_id);
create index on documents (tenant_id, sha256);

create table email_threads (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    text not null references tenants(id),
  message_id   text not null,
  from_addr    text not null,
  subject      text not null default '',
  body_text    text not null default '',
  received_at  timestamptz not null default now(),
  unique (tenant_id, message_id)
);

create table document_threads (
  document_id  uuid not null references documents(id) on delete cascade,
  thread_id    uuid not null references email_threads(id) on delete cascade,
  primary key (document_id, thread_id)
);

create table extractions (
  id                uuid primary key default gen_random_uuid(),
  document_id       uuid not null references documents(id) on delete cascade,
  tier              llm_tier not null,
  model_id          text not null,
  output_json       jsonb,
  refused           boolean not null default false,          -- ADDITION
  input_tokens      int not null default 0,
  cache_read_tokens int not null default 0,
  cache_write_tokens int not null default 0,                 -- ADDITION
  output_tokens     int not null default 0,
  cost_usd          numeric(12,8) not null default 0,
  latency_ms        int not null default 0,
  trace_id          text,
  created_at        timestamptz not null default now()
);
create index on extractions (document_id);

create table fields (
  id               uuid primary key default gen_random_uuid(),
  document_id      uuid not null references documents(id) on delete cascade,
  name             text not null,
  value            text,
  raw_conf         real,
  calibrated_conf  real,
  word_ids         int[] not null default '{}',
  box              int[] check (box is null or array_length(box, 1) = 4),
  page             int not null default 0,                   -- ADDITION
  final            boolean not null default false
);
create index on fields (document_id, name);
create unique index fields_one_final on fields (document_id, name) where final;

create table screen_results (
  id           uuid primary key default gen_random_uuid(),
  document_id  uuid not null references documents(id) on delete cascade,
  detector     text not null,
  verdict      text not null check (verdict in ('clean','suspicious','malicious')),
  categories   text[] not null default '{}',                 -- ADDITION
  spans_json   jsonb not null default '[]',
  created_at   timestamptz not null default now()
);

create table rule_results (
  id           uuid primary key default gen_random_uuid(),
  document_id  uuid not null references documents(id) on delete cascade,
  rule_id      text not null,
  passed       boolean not null,
  severity     text not null check (severity in ('pass','review','flag')),
  message      text not null,
  details_json jsonb not null default '{}',
  evaluation   int not null default 1,                       -- ADDITION: re-validation round
  created_at   timestamptz not null default now()
);
create index on rule_results (document_id, evaluation);

-- ---------------------------------------------------------------- human actions
create table reviews (
  id           uuid primary key default gen_random_uuid(),
  document_id  uuid not null references documents(id) on delete cascade,
  field_name   text not null,
  old_value    text,
  new_value    text,
  reviewer_id  uuid not null references staff_users(id),
  created_at   timestamptz not null default now()
);

create table approvals (
  id                uuid primary key default gen_random_uuid(),
  document_id       uuid not null references documents(id) on delete cascade,
  kind              approval_kind not null,
  approver_id       uuid not null references staff_users(id),
  callback_attested boolean not null default false,
  decision          approval_decision not null,
  note              text,                                     -- ADDITION
  created_at        timestamptz not null default now(),
  unique (document_id, kind, approver_id)
);

-- approver != uploader, enforced in the database (US-5, invariant 3).
create function approvals_not_uploader() returns trigger language plpgsql as $$
begin
  if exists (select 1 from documents d where d.id = new.document_id and d.uploaded_by = new.approver_id) then
    raise exception 'approver % uploaded document %; self-approval is not allowed', new.approver_id, new.document_id
      using errcode = 'check_violation';
  end if;
  if new.kind = 'bank_change' and new.decision = 'approve' and not new.callback_attested then
    -- each bank-change approval row must carry the callback attestation
    raise exception 'bank_change approval requires callback_attested' using errcode = 'check_violation';
  end if;
  return new;
end $$;
create trigger approvals_not_uploader before insert or update on approvals
  for each row execute function approvals_not_uploader();

create table bills (
  id               uuid primary key default gen_random_uuid(),
  document_id      uuid not null references documents(id),
  system           text not null check (system in ('qbo','xero','fake')),
  external_id      text,
  idempotency_key  text not null unique,
  status           text not null check (status in ('pending','posted','failed','deleted')),
  attachment_id    text,                                     -- ADDITION
  posted_at        timestamptz,
  created_at       timestamptz not null default now()
);

create table followups (
  id            uuid primary key default gen_random_uuid(),
  document_id   uuid not null references documents(id) on delete cascade,
  to_addr       text not null default '',                    -- ADDITION: from vendor master only
  draft_subject text not null,
  draft_body    text not null,
  approved_by   uuid references staff_users(id),
  approved_at   timestamptz,                                 -- ADDITION
  sent_at       timestamptz,
  created_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------- audit (append-only, hash-chained)
create table audit_log (
  id           bigserial primary key,
  ts           timestamptz not null,
  actor        text not null,          -- 'system' | 'user:<id>' | 'mcp:<client>'
  tenant_id    text,                   -- ADDITION
  document_id  uuid,                   -- no FK: audit rows outlive purged visitor documents
  action       text not null,
  before_json  jsonb,
  after_json   jsonb,
  prev_hash    text not null,
  row_hash     text not null unique
);
create index on audit_log (document_id);

create function audit_log_append_only() returns trigger language plpgsql as $$
begin
  raise exception 'audit_log is append-only (% not allowed)', tg_op using errcode = 'insufficient_privilege';
end $$;
create trigger audit_log_no_update before update or delete on audit_log
  for each row execute function audit_log_append_only();
create trigger audit_log_no_truncate before truncate on audit_log
  for each statement execute function audit_log_append_only();

-- ---------------------------------------------------------------- evals
create table eval_examples (
  id           uuid primary key default gen_random_uuid(),
  split        eval_split not null,
  document_id  uuid references documents(id) on delete set null,
  external_ref text,                                         -- ADDITION: data/gold file id
  gold_json    jsonb not null,
  provenance   text not null,          -- 'synthetic' | 'human_correction' | 'sroie' | 'cord' ...
  created_at   timestamptz not null default now()
);

create table eval_runs (
  id          uuid primary key default gen_random_uuid(),
  git_sha     text not null,
  config      text not null,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,                                   -- ADDITION
  totals_json jsonb not null default '{}'
);

create table eval_results (
  run_id       uuid not null references eval_runs(id) on delete cascade,
  example_id   uuid not null references eval_examples(id),
  repeat       int not null default 0,                       -- ADDITION: n=3 repeats
  metrics_json jsonb not null,
  cost_usd     numeric(12,8) not null default 0,
  latency_ms   int not null default 0,
  root_cause   text,
  primary key (run_id, example_id, repeat)
);

-- ---------------------------------------------------------------- ops
create table spend_ledger (
  day    date not null,
  scope  spend_scope not null,
  key    text not null,
  usd    numeric(12,8) not null default 0,
  primary key (day, scope, key)
);

-- ADDITION: Postgres SKIP LOCKED job queue (§4.1).
create table jobs (
  id           bigserial primary key,
  kind         text not null,
  document_id  uuid references documents(id) on delete cascade,
  payload      jsonb not null default '{}',
  dedupe_key   text,
  status       job_status not null default 'queued',
  attempts     int not null default 0,
  max_attempts int not null default 5,
  run_after    timestamptz not null default now(),
  locked_by    text,
  locked_at    timestamptz,
  last_error   text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index jobs_ready on jobs (run_after, id) where status = 'queued';
create unique index jobs_dedupe on jobs (dedupe_key) where dedupe_key is not null and status in ('queued','running');

-- ADDITION: comparison mode results (naive pipeline N vs InvoiceDesk), §M13.
create table comparison_results (
  id           uuid primary key default gen_random_uuid(),
  batch_id     uuid not null references batches(id) on delete cascade,
  document_id  uuid not null references documents(id) on delete cascade,
  pipeline     text not null check (pipeline in ('naive','invoicedesk')),
  outcome      text not null,          -- 'posted' | 'held' | ...
  verdict      text not null check (verdict in ('correct','wrong')),
  reason       text not null default '',
  amount_cents bigint,
  unique (document_id, pipeline)
);

-- realtime: NOTIFY on document status change (Supabase Realtime also replicates `documents`).
create function documents_notify() returns trigger language plpgsql as $$
begin
  perform pg_notify('invoicedesk_events', json_build_object(
    'type', 'document.status_changed', 'tenant_id', new.tenant_id, 'document_id', new.id,
    'batch_id', new.batch_id, 'status', new.status)::text);
  return new;
end $$;
create trigger documents_notify after update of status on documents
  for each row when (old.status is distinct from new.status) execute function documents_notify();
