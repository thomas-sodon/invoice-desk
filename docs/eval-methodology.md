# Eval methodology

How InvoiceDesk's accuracy, safety, cost and latency are measured, and what the published numbers
do and do not mean. The spec is [DESIGN §5.6](../DESIGN-InvoiceDesk.md#56-evaluation-plan). The
harness and its commands are in [evals/README.md](../evals/README.md).

> **Status on 2026-10-24: no live model numbers exist yet.** Every run so far used the offline
> gold-backed `OracleLLM` because this build environment has no API key. Offline reports exercise
> the harness, the real preprocessor and OCR, the screeners, the rules and the routing. They are
> **not** measurements of Claude's accuracy, cost or latency. Every offline report, badge and
> Report Card says so. See [docs/results](results/README.md) for the list of runs.

## 1. What we are trying to show

A buyer needs three answers:

1. **Does it read invoices correctly?** Field and line-item accuracy, plus calibration (when it
   says 95% sure, is it right 95% of the time?).
2. **Does it ever pay something it shouldn't?** False straight-through processing and the
   adversarial set. This is the hard gate.
3. **What does it cost and how fast is it?** $/document and latency per routing config.

## 2. Datasets

| Split | Size | Source | Use |
|---|---|---|---|
| `dev` | 60 | synthetic-core, frozen | tuning thresholds, prompts and the calibrator |
| `test` | 60 | synthetic-core, frozen | the experiment matrix only; **never looked at while tuning** |
| `adversarial` | 30 cases + 8 priors | scripted attacks (below) | zero-miss CI gate |
| `smoke` | 30 dev + 30 adversarial | deterministic stratified sample | PR gate |
| `public` | SROIE, CORD v1 test | download scripts only; images never redistributed | data we did not author |
| `correction` | grows | review-queue corrections (`provenance=human_correction`) | regression cases |
| `demo` | 25 | the demo batch | **excluded from every eval split** |

synthetic-core is 120 invoices stratified by medium: native PDF 40, scanned 40, phone photo 25,
handwritten 15. Truth comes first: the generator samples a work order, writes the invoice truth
(the gold label), then renders it with one of 8 vendor templates. Gold boxes come from the DOM at
render time. The split hashes are frozen and verified (`make data-splits`).

The adversarial set has 4 white-text PDFs, 3 PDFs with 2 pt text, 2 with tiny fonts and 2 with
text off the page; 3 faint-text photos; 3 email-body injections; 3 lookalike letterheads;
2 legitimate-looking bank-change letters; 6 duplicates (2 re-photographed, 2 with a tweaked
amount, 2 with a new invoice number); and 2 split invoices under the cap.

**Known limits of the data** (all in [GAPS.md](GAPS.md)):

- Phone photos and handwriting are *simulated*: a recorded homography, lighting, blur and noise
  for photos, and a Hershey stroke font with jitter for handwriting. Real printed-and-photographed
  documents and hand-filled carbon books have not been made yet.
- SROIE and CORD were not downloaded here.
- One label gap is known: `core-117`.

## 3. Metrics

Definitions are exact in [evals/README.md](../evals/README.md#metric-definitions). In short:

| Metric | Definition | Target (§5.6) |
|---|---|---|
| Header-field micro F1 | 13 fields, normalized exact match (cents, ISO date, resolved `vendor_id`, exact WO#), null-vs-value counted properly | >= 0.96 |
| Line-item F1 | Hungarian alignment on description fuzzy >= 0.8 and exact amount | >= 0.90 |
| Document exact match | every required field right | >= 85% |
| STP rate | clean-truth documents that reached ReadyToPay with no human touch | >= 70% |
| **False-STP rate** | auto-posted documents with a wrong required field or a missed rule violation | **<= 1%** (hard gate) |
| Rule P/R | per rule vs planted labels | duplicate recall 100%, precision >= 95% |
| Injection recall / FPR | on adversarial / on clean | 100% / <= 3% |
| Bank-change hold recall | | 100% |
| ECE | 10 equal-width bins of calibrated field confidence | <= 0.05 |
| Box hit rate | IoU >= 0.5 vs gold boxes, native and scanned only | >= 90% |
| $/doc | `usage` x the price table, summed over classifier and tiers | <= $0.015 routed |
| p50 / p95 latency | upload to final state | <= 4 s / <= 12 s |

The targets are targets. A report shows the measured value, its 95% CI and whether it meets the
target, and a miss is printed as a miss.

## 4. Experiment matrix

`evals/configs/matrix.yaml`, n=3 repeats each, scored on `test`:

| Config | What it isolates |
|---|---|
| A | Opus 5.5 only, with caching: the "just use the strongest model" baseline |
| B | Sonnet 5.5 only |
| C | Haiku 5.5 only |
| **D** | **Routed Haiku -> Sonnet -> Opus, with caching (production)** |
| D' | D without prompt caching: the caching saving |
| E | OCR text only + Haiku, no image |
| F | native-PDF text-layer path (native PDFs only) |
| G | Azure DI prebuilt-invoice alone: commercial baseline (needs Azure keys) |
| N | naive baseline: one prompt, no rules, auto-post everything (powers comparison mode) |

Config knobs are applied by wrapping the LLM client around the unchanged production pipeline, so
every config runs through the same preprocessing, screening, rules and routing code.

## 5. Statistics

- **Why n=3.** The 5.5 models reject sampling parameters, so outputs cannot be pinned with
  `temperature=0`. Each config runs three times.
- **CIs.** 95% percentile *cluster* bootstrap: documents are resampled with all their repeats
  (1,000 resamples, seeded), so repeats don't count as independent evidence.
- **Comparisons.** D vs A (and D vs N) use a paired bootstrap over shared documents. Reports
  show the delta, its CI and a p-value.
- **Offline caveat.** The oracle is deterministic, so offline repeats are identical and the CIs
  reflect document sampling only.

## 6. Tuning without leaking the test split

- `tau_field` and `tau_hi` are chosen by `invoicedesk-eval fit-thresholds`, which refuses any
  split except exactly `dev` *before* it runs anything. `--thresholds` refuses files not fitted
  on dev.
- The calibrator (`invoicedesk.calibrate.fit_calibrator`) refuses non-dev splits.
- Test labels are never edited. The demo batch is in no split.
- The thresholds currently in `tenants/maple_row/rules.yaml` (0.90 / 0.95) are placeholders,
  not fitted values. An offline fit is meaningless, because the oracle never misreads a required
  field on dev, so none has been promoted.

## 7. Root causes

Every failure gets one label: OCR misread, wrong field mapping, line-item table split,
handwriting illegible, rule false positive, or calibration miss (plus rule false negative,
screening false positive, pipeline error, other). Live runs use Opus 5.5 at effort `high` through
the Batch API, on a **masked, structured failure summary** (never raw document text). Offline runs
use a deterministic heuristic. A seeded sample goes to a spot-check CSV for a human; §5.6 asks
for 20%. No human spot check has been done yet.

## 8. CI gates

| Workflow | When | What |
|---|---|---|
| `ci.yml` | push, PR | ruff, pyright, pytest (rules at 100% branch coverage), web typecheck and lint |
| `eval-gate.yml` | PR | offline smoke vs `evals/baselines/smoke-offline.json`. The live smoke (~$0.70) runs only when the `ANTHROPIC_API_KEY` secret exists. |
| `nightly-eval.yml` | nightly | C and D live on all 120 docs (n=3), Langfuse dataset push, badge, report, Report Card |
| `public-benchmark.yml` | weekly | SROIE + CORD, live only |

A PR fails if field F1 drops more than 1.0 pt vs `main`, false-STP exceeds 1%, any adversarial
case is missed, or $/doc rises more than 20%. A run that crashes on a document counts that
document as unread, so breaking the pipeline cannot improve F1 or $/doc. The gate's failure path
is tested with deliberately broken baselines. The workflows have not run on GitHub yet.

**What the offline gate can and cannot catch.** It catches regressions in preprocessing,
screening, rules, routing and the harness. It cannot catch a prompt regression, because the
oracle ignores prompts. That needs the live smoke job.

## 9. How results get published

1. A run writes `eval_runs` and `eval_results` rows, plus `evals/reports/<date>-<split>-<configs>-<mode>.{json,md}`.
2. `make report-card` renders the AP Automation Report Card PDF from the latest run.
3. README and portfolio copy quote **only** values from the latest report JSON, link the file, and
   say "benchmark on synthetic data". An offline run is labelled as an offline harness run and is
   never quoted as model accuracy.
4. The bracketed figures in the DESIGN §9-§10 demo script are filled only after a live test-split run.
