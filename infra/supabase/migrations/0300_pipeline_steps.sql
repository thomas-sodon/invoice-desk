-- pipeline component (0300-0399).
--
-- pipeline_steps: one row per completed pipeline node per document (and per validation round).
-- A node writes its DB effects, its status transition and this row in ONE transaction, so a node
-- that is re-run after a crash (its LangGraph checkpoint was not written yet) finds the row and
-- returns the stored output instead of writing duplicate extractions/fields/rule results.
-- LangGraph's own checkpoint tables are created by PostgresSaver.setup().
create table if not exists pipeline_steps (
  document_id  uuid not null references documents(id) on delete cascade,
  step         text not null check (step in ('preprocess','screen','extract','validate','post')),
  round        int  not null default 0,
  output_json  jsonb not null default '{}',
  created_at   timestamptz not null default now(),
  primary key (document_id, step, round)
);

-- Comparison mode (M13): what the naive pipeline read (masked) so verdicts can be recomputed.
alter table comparison_results add column if not exists details_json jsonb not null default '{}';
alter table comparison_results add column if not exists updated_at timestamptz not null default now();
