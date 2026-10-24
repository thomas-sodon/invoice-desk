# data: synthetic Maple Row data (DESIGN §6.2, §5.6 datasets)

Everything here is fictional and all of it is synthetic:

- Maple Row Property Management, its 6 properties, 40 units and 30 vendors.
- Phone numbers use the reserved 555-01xx range, and e-mail addresses use `.example` domains.
- Bank details use the test routing number `110000000` with `000999...` accounts (vendor master)
  or `000555...` accounts (attackers). The vendor master stores only `remit_to_hash` and
  `bank_last4`.

| Path | What | Committed |
|---|---|---|
| `invoicedesk_data/` | the `invoicedesk-data` package (see below) | yes |
| `seed/*.csv` | business universe: properties, units, vendors, work orders, estimate lines, 18 months of historical bills, staff | yes |
| `gold/synthetic-core/` | 120 gold labels plus `manifest.json`, **frozen** 60 dev / 60 test | yes |
| `gold/adversarial/` | 30 cases + 8 priors (duplicates are judged against a prior) | yes |
| `gold/demo/` | gold for the 25 demo files + the e-mail attachment | yes |
| `demo/` | the 25-file demo batch, `expected.json`, `bank-change-email.json`, `email/` | yes |
| `generated/` | synthetic-core and adversarial renders (about 25 MB) | no, rebuild with `make data-generate data-adversarial` |
| `public/cache/` | SROIE / CORD downloads | **never** |

## Commands

```bash
make seed               # load the universe into $DATABASE_URL (idempotent) + write data/seed/*.csv
make data-generate      # synthetic-core: 40 native, 40 scanned, 25 photo, 15 handwritten
make data-adversarial   # the 30-case adversarial set (§5.6)
make data-demo          # rebuild data/demo/ byte for byte
make data-splits        # verify the frozen split (hashes, stratification, generator drift)
make data-verify        # label QA: route every gold label through the real rules engine
make data-eval-examples # load gold into eval_examples
make data-public        # download SROIE + CORD into data/public/cache
```

## Pieces

- **`universe.py`, `seed.py`**: `build_universe()` uses a fixed seed. `seed_database(db,
  tenant_id="maple_row")` is idempotent (deterministic UUIDs). The demo entities are:
  - Rapid Rooter Plumbing LLC;
  - WO-1187 (Unit 12B, estimate $3,200);
  - WO-1179 (Unit 10A);
  - INV-2231 ($1,240.00, 2026-10-10);
  - the lookalike "Rapid Rotor Plumbing LLC", which is not in the vendor master.
- **`generator/`**:
  - truth-first sampler with planted defects (`DEFECTS` maps each to the rules it must fail);
  - 8 Jinja vendor templates (trades, carbon_copy, quickbooks, word, modern, letterhead,
    compact, statement) plus a bank-change letter;
  - Playwright render to PDF, with gold boxes read from the DOM, scaled to the preprocessor's
    1,600 px geometry and tightened to the ink;
  - scan and phone-photo simulation with recorded affine or homography transforms;
  - Hershey-script handwriting.
- **`adversarial.py`**: white text, 2 pt, faint photo, e-mail-body injection, lookalike
  letterhead, bank-change letter, duplicates (re-photo, tweaked amount, new number), split
  invoice, off-page text, tiny font.
- **`demo.py`**:
  - `demo_files()` returns the 25 `DemoFile`s in board order with their expected outcomes:
    18 Ready to pay, 4 Needs review, 3 Flagged;
  - `bank_change_email()` returns the extra e-mail;
  - `DEMO_TODAY` is the date the batch must be evaluated at.
- **`oracle.py`**: `OracleLLM`, a deterministic gold-backed `LLMClient`.
  - It recognizes documents by file or processed-page hash, or by OCR text.
  - Its extraction has per-tier noise, so escalation gets exercised.
  - Its injection classifier is keyword-based.
- **`verify.py`, `frozen.py`**: the rules self-check and the frozen-split integrity check.
- **`public/`**: SROIE / CORD downloaders and the mappings to vendor, date and total.

CLAUDE.md applies: never look at, tune on, or edit the `test` split; the demo batch is excluded
from every eval split. Known substitutions (simulated photos and handwriting, and others) are
listed in `docs/GAPS.md`.
