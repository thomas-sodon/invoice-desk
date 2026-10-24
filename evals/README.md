# InvoiceDesk evals

The eval harness, metrics, experiment matrix, CI gate, root-cause labeling and the AP Automation
Report Card (DESIGN §5.6, §7; features M6, M17, M18, M19, M21). Every number is a
**benchmark on synthetic data**. No client results are claimed.

## Quick start

```bash
make eval-offline                    # $0: D + N on dev+adversarial, n=3, report + Report Card PDF
make eval-smoke                      # $0: the PR gate locally (smoke set vs committed baseline)
make eval-estimate CONFIG=all        # what a live run would cost
INVOICEDESK_MODE=live make eval CONFIG=D SPLIT=smoke REPEATS=1 \
    EVAL_FLAGS=--i-know-this-costs-money          # ~$0.70, needs ANTHROPIC_API_KEY
make report-card                     # re-render the PDF from the latest run
```

`invoicedesk-eval --help` lists every subcommand (`run`, `fit-thresholds`, `gate`, `baseline`,
`report-card`, `public`, `estimate`, `smoke-list`).

## Offline vs live

* **Offline** (default): model answers come from `invoicedesk_data.oracle.OracleLLM`, a
  deterministic gold-backed client with per-tier noise. Everything else is the real thing: the
  real preprocessor (PDF render, RapidOCR, text layer, pHash), the real guardrail screeners,
  the tiered extractor and calibrator, the rules engine and the LangGraph pipeline. Offline
  numbers test the harness and the deterministic parts. They are **not** measurements of
  Claude's accuracy, cost or latency, and every offline report, badge and Report Card says so.
  Costs are computed from the oracle's synthetic token counts and the real price table;
  latency is wall-clock plus the oracle's simulated per-tier call times.
* **Live**: `INVOICEDESK_MODE=live` (or `--live`) uses `wiring.llm_client`. The runner prints
  a cost estimate (DESIGN §7.3 budgets per config) and refuses to start without
  `--i-know-this-costs-money`. The full matrix (~$40), and any live run estimated above $15
  (e.g. every config but N), also needs `--full-matrix`, after asking the owner (CLAUDE.md).

## How a run works

1. The split is loaded from the committed gold (`data/gold`); binaries are rebuilt by
   `invoicedesk-data generate` / `adversarial` (gitignored).
2. Each document runs in its **own workspace database** cloned from a migrated, seeded
   template, with its declared priors (the originals of duplicate cases) processed first. A
   document is therefore checked for duplicates against the seeded 18-month history and its
   priors only, which is exactly how the labels were made.
3. The pipeline runs in-process (no job queue, in-memory checkpointer). `ReadyToPay` is the
   auto-post decision being measured; nothing is posted.
4. Config knobs are applied around the pipeline (`harness.ConfigLLM`): single-tier configs
   remap the tier-1 call's role to the chosen model; D' strips cache breakpoints; E and F
   strip page images; G swaps in the Azure DI reader; N calls the naive baseline's prompt.
5. Each (example, repeat) is scored against its gold label (`scoring.score_run`) and written
   to `eval_results`; `eval_runs.totals_json` holds the scorecard shape
   (`{"metrics": {name: {"value", "ci"}}, "details", "reliability", ...}`).
6. Reports go to `evals/reports/<date>-<split>-<configs>-<mode>.{json,md}` with a reliability
   PNG and a root-cause spot-check CSV per config.

## Metric definitions

| Metric | Definition |
|---|---|
| Header-field micro F1 | 13 fields; TP = normalized exact match (cents, ISO date, resolved vendor_id, exact WO#); gold value vs wrong value = FP + FN, missing = FN, spurious = FP, both null = not counted. Fields the label marks illegible are excluded. Quarantined documents are not read, so they are not field-scored. |
| Line-item F1 | Hungarian alignment (`scipy.optimize.linear_sum_assignment`) on description fuzzy ratio >= 0.8 and exact amount |
| Document exact match | every required field right, over extracted documents |
| STP rate | clean-truth documents that ended `ReadyToPay` / clean-truth documents |
| False-STP rate | auto-posted documents with a wrong (or unread) required field or a held gold label / auto-posted documents |
| Rule P/R | per rule vs `expected.rules`; a quarantined document is scored on R10 only |
| Injection recall / FPR | quarantined or R10 on gold-R10/quarantine documents / on clean-truth documents |
| Bank-change hold recall | documents whose label fails R6 that ended Flagged with R6 failed (or quarantined) |
| Adversarial misses | of the 30 cases: final status differs from the label, or a planted `flag` rule did not fire |
| ECE | 10 equal-width bins over (calibrated confidence, field right) for every read header field |
| Box hit rate | IoU >= 0.5 and same page vs gold boxes, header fields, native and scanned only |
| $/doc, p50/p95 | per-document sum of call costs (classifier + tiers); latency from upload to final state |

CIs are 95% percentile **cluster bootstrap** intervals (documents resampled with all their
repeats; seeded). D vs A (and D vs N) use a paired bootstrap over shared documents.

## Splits and the test-split rule

`dev` / `test` are the frozen 60/60 halves of synthetic-core. **Tuning happens on dev only**:
`invoicedesk-eval fit-thresholds` refuses any split other than exactly `dev` before running, and
delegates to `invoicedesk.calibrate.choose_thresholds` (which refuses non-dev splits too). Its
JSON can be applied to a run with `--thresholds`, which refuses files not fitted on dev. The
matrix itself is *scored* on `test`. `smoke` = 30 stratified dev documents (by medium, mixing
clean and held labels; deterministic) + the 30 adversarial cases. The demo batch is in no split.

## CI

* Scoring of failures: a document the run should have read but did not (harness error,
  refusal, missing extraction) counts as *unread* (every gold field a miss), and the LLM spend
  of a failed run is kept, so a change that makes documents crash cannot raise F1 or lower
  $/doc. The gate also fails when the baseline's mode or split differs from the current run.
* `eval-gate.yml` (PRs): unit tests, then the offline smoke run gated against main's
  `evals/baselines/smoke-offline.json`; the live smoke (~$0.70) runs only when the
  `ANTHROPIC_API_KEY` secret exists, gated against `evals/baselines/smoke-live.json`.
* `nightly-eval.yml`: C and D on `all` (n=3) live, Langfuse dataset push, badge, reports,
  Report Card and the live smoke baseline committed. Without a key: offline check, no commit.
* `public-benchmark.yml` (weekly): SROIE + CORD, live only.

Gate: field F1 drop <= 1.0 pt vs main, false-STP <= 1%, zero adversarial misses (at least one
case scored), $/doc rise <= 20%.

## Root causes

Failures are labeled ocr_misread, wrong_field_mapping, line_item_table_split,
handwriting_illegible, rule_false_positive, calibration_miss (plus rule_false_negative,
screening_false_positive, pipeline_error, other). Live runs use Opus (`ModelRole.ROOT_CAUSE`,
effort high) through the Batch API on a masked, structured failure summary (never raw document
text); offline runs use a deterministic heuristic. A seeded sample goes to a spot-check CSV with
blank `human_label` / `agrees` columns.
