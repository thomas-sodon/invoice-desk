-- evals component (0600-0699): run metadata for the scorecard and report card (M17, M21).
-- eval_runs.totals_json holds {"metrics": {name: {"value", "ci": [lo, hi]}}, "details", ...}.
-- `mode` separates offline runs (deterministic gold-backed oracle; never a model measurement)
-- from live runs against the real models, so the public scorecard can show live rows only.
alter table eval_runs add column if not exists split text;
alter table eval_runs add column if not exists mode text not null default 'offline'
  check (mode in ('offline', 'live'));
alter table eval_runs add column if not exists n_repeats int not null default 1;
alter table eval_runs add column if not exists report_path text;
create index if not exists eval_runs_latest on eval_runs (mode, finished_at desc nulls last);
create index if not exists eval_results_example on eval_results (example_id);
