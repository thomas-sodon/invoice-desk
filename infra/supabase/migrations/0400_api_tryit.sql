-- api component (0400-0499).
--
-- tryit_counters: per-day request counters for the public try-it page (M14, §5.5 Ops row).
-- Scopes: 'ip_upload' and 'ip_session' (key = salted SHA-256 of the client IP, never the raw IP),
-- 'session_upload' (key = try-it session id) and 'ip_demo' (anonymous demo runs per IP).
-- Dollar spend lives in spend_ledger (0001); this table only counts requests.
create table if not exists tryit_counters (
  day    date not null,
  scope  text not null check (scope in ('ip_upload', 'ip_session', 'session_upload', 'ip_demo')),
  key    text not null,
  n      int  not null default 0 check (n >= 0),
  primary key (day, scope, key)
);

-- The purge job scans expired visitor documents.
create index if not exists documents_visitor_purge on documents (purge_after)
  where session_id is not null;

-- demo_runs: how a demo batch (batches.mode = 'demo') was requested (US-8). ``llm`` picks the
-- extraction client for the batch: 'replay' = the recorded/gold-backed run (no LLM cost),
-- 'live' = real Claude calls (staff only, INVOICEDESK_MODE=live). Deleted with the batch by
-- make demo-reset (cascade).
create table if not exists demo_runs (
  batch_id      uuid primary key references batches(id) on delete cascade,
  llm           text not null check (llm in ('live', 'replay')),
  requested_by  text not null,          -- audit label: 'user:<id>' or 'anonymous'
  created_at    timestamptz not null default now()
);
