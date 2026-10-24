# Tenants

Each tenant has a folder `tenants/<tenant_id>/` with a `rules.yaml` that configures the
deterministic rules engine R1-R10 (`invoicedesk.rules`, DESIGN §5.5). The schema is
`invoicedesk.config.tenant.RulesConfig`: unknown keys are an error, amounts are integer cents
and percentages are basis points (100 bps = 1 %). Only the confidence thresholds (`tau_*`) are
probabilities, and those are tuned on the **dev** split only.

Load it with `invoicedesk.config.tenant.load_rules_config("<tenant_id>")`. The database keeps a
copy in `tenants.rules_yaml` (parsed with `parse_rules_yaml`).

## What each rule reads

| Rule | Config | Fails as |
|---|---|---|
| R1 vendor_known | `vendor.fuzzy_match_min_score`, `vendor.lookalike_max_edit_distance`, `vendor.check_homoglyphs` | lookalike or blocked: **flag**; unknown, pending or tax-ID mismatch: review (a `new_vendor` approval from a non-uploader clears unknown and pending) |
| R2 work_order_exists | none | review |
| R3 three_way_match | `three_way.tolerance_bps`, `three_way.tolerance_min_cents` | review, with the deterministic variance sentence |
| R4 duplicate | `duplicate.fuzzy_amount_bps`, `duplicate.fuzzy_window_days`, `duplicate.phash_max_distance` | **flag** "Duplicate of INV-xxxx" |
| R5 arithmetic | `arithmetic.rounding_slack_cents`, `arithmetic.tax_rate_min_bps`, `arithmetic.tax_rate_max_bps` | review |
| R6 bank_change | **none, by design** (CLAUDE.md invariant 3) | **flag**, always; released only by two approvers plus a callback |
| R7 amount_cap | `amount_cap_cents` | review (an `over_cap` approval from a non-uploader clears it) |
| R8 date_sanity | `dates.max_age_days`, `dates.allow_future_days` | review |
| R9 confidence | `confidence.required_fields`, `confidence.tau_field`, `confidence.per_field` | review, with the low fields highlighted |
| R10 injection | none | **flag** |

Notes:

- R3 compares the **pre-tax** amount (subtotal, else the sum of line amounts, else the total)
  with `work_orders.approved_estimate_cents`. The tolerance is inclusive: on a $3,400.00
  estimate, $340.00 over passes and $340.01 over is held.
- R9 fails closed: a required field without a calibrated confidence is held, and the model's
  raw self-report never clears it.
- `hours_saved_per_invoice_min` and `manual_cost_per_invoice_cents` feed the owner dashboard,
  not the rules.

Try a config locally:

```bash
uv run invoicedesk validate extraction.json tenant_data.json --tenant maple_row --today 2026-10-24 --raw-conf
```
