# Results index

Every published number must come from one of the report files listed here. The eval harness
writes them to [`evals/reports/`](../../evals/reports/); this page records what each one is and how
far it can be trusted. The method is in [eval-methodology.md](../eval-methodology.md).

| Date | Report | Mode | Split | Configs | What it can support |
|---|---|---|---|---|---|
| 2026-10-24 | [2026-10-24-dev_adversarial-D-N-offline.md](../../evals/reports/2026-10-24-dev_adversarial-D-N-offline.md) ([JSON](../../evals/reports/2026-10-24-dev_adversarial-D-N-offline.json)) | **offline (OracleLLM)** | dev + adversarial | D, N | Pipeline, screening, rules and routing behaviour with gold-backed reads. **Not** Claude accuracy, cost or latency. |

## Live runs

None yet. There was no `ANTHROPIC_API_KEY` in the build environment ([GAPS.md](../GAPS.md),
Evals section). When the first live run lands, add a row here, refresh the README results table
from its JSON, and replace the offline note.

The planned first live runs:

1. `INVOICEDESK_MODE=live make eval CONFIG=D SPLIT=smoke REPEATS=1 EVAL_FLAGS=--i-know-this-costs-money`
   (about $0.70).
2. The nightly C and D run on all 120 documents.
3. The full matrix on `test` (about $40). The owner must approve this one first.

## Report Card

[AP-Automation-Report-Card.pdf](../report-card/AP-Automation-Report-Card.pdf) is rendered from the
latest run by `make report-card`. It currently carries the offline banner.
