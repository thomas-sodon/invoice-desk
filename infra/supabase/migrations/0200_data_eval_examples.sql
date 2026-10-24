-- data component (0200-0299): gold labels are loaded into eval_examples idempotently, keyed by
-- (split, external_ref) where external_ref = '<set>/<doc_id>' (data/gold/<set>/<doc_id>.json).
create unique index if not exists eval_examples_split_external_ref
  on eval_examples (split, external_ref) where external_ref is not null;
