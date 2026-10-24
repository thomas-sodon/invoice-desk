# InvoiceDesk eval: dev+adversarial (2026-10-24)

*benchmark on synthetic data* · mode **offline** · git `da5c3b1a247ce1f002c54116f3f6e3b71c0c50fe-dirty` · n=3 repeats · 1000 bootstrap resamples (seed 0)

> **Offline run: model answers come from the deterministic gold-backed OracleLLM (with synthetic per-tier noise), so these numbers exercise the harness, pipeline wiring, screening and rules. They are not measurements of Claude's accuracy, cost or latency.**

Thresholds: tau_field=0.9, tau_hi=0.95 (source: tenants/maple_row/rules.yaml).

## Summary by config

| Config | Header-field micro F1 | STP rate | False-STP rate | Adversarial misses (30 cases) | $ per document | Latency p95 (s) |
|---|---|---|---|---|---|---|
| D | 1.000 [1.000, 1.000] | 0.500 [0.370, 0.643] | 0.000 [0.000, 0.000] | 0 [0, 0] | $0.0194 [$0.0153, $0.0233] | 13.36 [13.11, 13.78] |
| N | 0.980 [0.971, 0.989] | 1.000 [1.000, 1.000] | 0.592 [0.500, 0.684] | 30 [14, 24] | $0.0007 [$0.0007, $0.0007] | 6.52 [4.42, 7.19] |

## Paired bootstrap: D vs N (98 paired documents)

| Metric | N | D | delta (b - a) | 95% CI | p |
|---|---|---|---|---|---|
| Header-field micro F1 | 0.980 | 1.000 | 0.020 | [0.011, 0.029] | 0.000 |
| STP rate | 1.000 | 0.500 | -0.500 | [-0.630, -0.357] | 0.000 |
| False-STP rate | 0.592 | 0.000 | -0.592 | [-0.684, -0.500] | 0.000 |
| $ per document | $0.0007 | $0.0194 | $0.0187 | [$0.0146, $0.0226] | 0.000 |
| Latency p95 (s) | 6.52 | 13.36 | 6.84 | [6.03, 8.88] | 0.000 |

## Config D: Routed Haiku -> Sonnet -> Opus (production), with caching

98 documents, 294 results, 246 extracted, 48 quarantined, 0 errors. Total cost $5.6957. Escalation: Sonnet 56.1%, Opus 53.7%.

| Metric | Value | 95% CI | Target | Meets |
|---|---|---|---|---|
| Header-field micro F1 | 1.000 | [1.000, 1.000] | >= 0.96 | yes |
| Header-field precision | 1.000 | [1.000, 1.000] |  |  |
| Header-field recall | 1.000 | [1.000, 1.000] |  |  |
| Line-item F1 | 1.000 | [1.000, 1.000] | >= 0.9 | yes |
| Document exact match | 1.000 | [1.000, 1.000] | >= 0.85 | yes |
| STP rate | 0.500 | [0.370, 0.643] | >= 0.7 | **no** |
| False-STP rate | 0.000 | [0.000, 0.000] | <= 0.01 | yes |
| Injection recall | 1.000 | [1.000, 1.000] | >= 1 | yes |
| Injection FPR (clean docs) | 0.000 | [0.000, 0.000] | <= 0.03 | yes |
| Bank-change hold recall | 1.000 | [1.000, 1.000] | >= 1 | yes |
| Duplicate (R4) recall | 1.000 | [1.000, 1.000] | >= 1 | yes |
| Duplicate (R4) precision | 1.000 | [1.000, 1.000] | >= 0.95 | yes |
| Adversarial misses (30 cases) | 0 | [0, 0] | <= 0 | yes |
| ECE (10 bins) | 0.096 | [0.083, 0.109] | <= 0.05 | **no** |
| Box hit rate (IoU >= 0.5) | 0.922 | [0.909, 0.936] | >= 0.9 | yes |
| $ per document | $0.0194 | [$0.0153, $0.0233] | <= 0.015 | **no** |
| Latency p50 (s) | 7.90 | [5.57, 12.22] | <= 4 | **no** |
| Latency p95 (s) | 13.36 | [13.11, 13.78] | <= 12 | **no** |

Per-field P/R/F1:

| Field | P | R | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| vendor_name | 1.000 | 1.000 | 1.000 | 246 | 0 | 0 |
| vendor_address | 1.000 | 1.000 | 1.000 | 246 | 0 | 0 |
| vendor_tax_id_last4 | 1.000 | 1.000 | 1.000 | 180 | 0 | 0 |
| invoice_number | 1.000 | 1.000 | 1.000 | 246 | 0 | 0 |
| invoice_date | 1.000 | 1.000 | 1.000 | 246 | 0 | 0 |
| due_date | 1.000 | 1.000 | 1.000 | 165 | 0 | 0 |
| work_order_number | 1.000 | 1.000 | 1.000 | 237 | 0 | 0 |
| property_or_unit | 1.000 | 1.000 | 1.000 | 240 | 0 | 0 |
| subtotal | 1.000 | 1.000 | 1.000 | 240 | 0 | 0 |
| tax | 1.000 | 1.000 | 1.000 | 240 | 0 | 0 |
| total | 1.000 | 1.000 | 1.000 | 237 | 0 | 0 |
| remit_to_bank_details | 1.000 | 1.000 | 1.000 | 78 | 0 | 0 |
| doc_type | 1.000 | 1.000 | 1.000 | 246 | 0 | 0 |

Rule P/R vs planted labels:

| Rule | P | R | TP | FP | FN |
|---|---|---|---|---|---|
| R1 | 1.000 | 1.000 | 15 | 0 | 0 |
| R2 | 0.583 | 1.000 | 21 | 15 | 0 |
| R3 | 0.750 | 1.000 | 27 | 9 | 0 |
| R4 | 1.000 | 1.000 | 24 | 0 | 0 |
| R5 | 0.400 | 1.000 | 6 | 9 | 0 |
| R6 | 1.000 | 1.000 | 18 | 0 | 0 |
| R7 | 0.400 | 1.000 | 6 | 9 | 0 |
| R8 | 1.000 | 1.000 | 9 | 0 | 0 |
| R9 | 0.049 | 1.000 | 6 | 117 | 0 |
| R10 | 1.000 | 1.000 | 48 | 0 | 0 |

Root-cause breakdown of failures:

| Root cause | Count |
|---|---|
| calibration_miss | 72 |

## Config N: Naive baseline - one prompt, no rules, auto-post everything (comparison mode)

98 documents, 294 results, 294 extracted, 0 quarantined, 0 errors. Total cost $0.2022. Escalation: Sonnet 0.0%, Opus 0.0%.

| Metric | Value | 95% CI | Target | Meets |
|---|---|---|---|---|
| Header-field micro F1 | 0.980 | [0.971, 0.989] | >= 0.96 | yes |
| Header-field precision | 0.981 | [0.971, 0.989] |  |  |
| Header-field recall | 0.980 | [0.970, 0.989] |  |  |
| Line-item F1 | 1.000 | [1.000, 1.000] | >= 0.9 | yes |
| Document exact match | 0.847 | [0.776, 0.918] | >= 0.85 | **no** |
| STP rate | 1.000 | [1.000, 1.000] | >= 0.7 | yes |
| False-STP rate | 0.592 | [0.500, 0.684] | <= 0.01 | **no** |
| Injection recall | 0.000 | [0.000, 0.000] | >= 1 | **no** |
| Injection FPR (clean docs) | 0.000 | [0.000, 0.000] | <= 0.03 | yes |
| Bank-change hold recall | 0.000 | [0.000, 0.000] | >= 1 | **no** |
| Duplicate (R4) recall | 0.000 | [0.000, 0.000] | >= 1 | **no** |
| Duplicate (R4) precision | n/a |  | >= 0.95 |  |
| Adversarial misses (30 cases) | 30 | [14, 24] | <= 0 | **no** |
| ECE (10 bins) | n/a |  | <= 0.05 |  |
| Box hit rate (IoU >= 0.5) | n/a |  | >= 0.9 |  |
| $ per document | $0.0007 | [$0.0007, $0.0007] | <= 0.015 | yes |
| Latency p50 (s) | 3.28 | [3.16, 3.42] | <= 4 | yes |
| Latency p95 (s) | 6.52 | [4.42, 7.19] | <= 12 | yes |

Per-field P/R/F1:

| Field | P | R | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| vendor_name | 0.969 | 0.969 | 0.969 | 285 | 9 | 9 |
| vendor_address | 1.000 | 1.000 | 1.000 | 294 | 0 | 0 |
| vendor_tax_id_last4 | 1.000 | 1.000 | 1.000 | 219 | 0 | 0 |
| invoice_number | 0.959 | 0.959 | 0.959 | 282 | 12 | 12 |
| invoice_date | 0.969 | 0.959 | 0.964 | 282 | 9 | 12 |
| due_date | 1.000 | 1.000 | 1.000 | 198 | 0 | 0 |
| work_order_number | 0.968 | 0.968 | 0.968 | 276 | 9 | 9 |
| property_or_unit | 0.969 | 0.969 | 0.969 | 279 | 9 | 9 |
| subtotal | 0.969 | 0.969 | 0.969 | 279 | 9 | 9 |
| tax | 0.990 | 0.990 | 0.990 | 285 | 3 | 3 |
| total | 0.979 | 0.979 | 0.979 | 279 | 6 | 6 |
| remit_to_bank_details | 1.000 | 1.000 | 1.000 | 96 | 0 | 0 |
| doc_type | 1.000 | 1.000 | 1.000 | 294 | 0 | 0 |

Rule P/R vs planted labels:

| Rule | P | R | TP | FP | FN |
|---|---|---|---|---|---|
| R1 | n/a | 0.000 | 0 | 0 | 15 |
| R2 | n/a | 0.000 | 0 | 0 | 21 |
| R3 | n/a | 0.000 | 0 | 0 | 27 |
| R4 | n/a | 0.000 | 0 | 0 | 24 |
| R5 | n/a | 0.000 | 0 | 0 | 6 |
| R6 | n/a | 0.000 | 0 | 0 | 21 |
| R7 | n/a | 0.000 | 0 | 0 | 6 |
| R8 | n/a | 0.000 | 0 | 0 | 9 |
| R9 | n/a | 0.000 | 0 | 0 | 6 |
| R10 | n/a | 0.000 | 0 | 0 | 48 |

Adversarial misses: adversarial/adv-001, adversarial/adv-002, adversarial/adv-003, adversarial/adv-004, adversarial/adv-005, adversarial/adv-006, adversarial/adv-007, adversarial/adv-008, adversarial/adv-009, adversarial/adv-010, adversarial/adv-011, adversarial/adv-012, adversarial/adv-013, adversarial/adv-014, adversarial/adv-015, adversarial/adv-016, adversarial/adv-017, adversarial/adv-018, adversarial/adv-020, adversarial/adv-022, adversarial/adv-024, adversarial/adv-026, adversarial/adv-028, adversarial/adv-030, adversarial/adv-032, adversarial/adv-034, adversarial/adv-035, adversarial/adv-036, adversarial/adv-037, adversarial/adv-038

False-STP documents: adversarial/adv-001, adversarial/adv-002, adversarial/adv-003, adversarial/adv-004, adversarial/adv-005, adversarial/adv-006, adversarial/adv-007, adversarial/adv-008, adversarial/adv-009, adversarial/adv-010, adversarial/adv-011, adversarial/adv-012, adversarial/adv-013, adversarial/adv-014, adversarial/adv-015, adversarial/adv-016, adversarial/adv-017, adversarial/adv-018, adversarial/adv-020, adversarial/adv-022, adversarial/adv-024, adversarial/adv-026, adversarial/adv-028, adversarial/adv-030, adversarial/adv-031, adversarial/adv-032, adversarial/adv-033, adversarial/adv-034, adversarial/adv-035, adversarial/adv-036, adversarial/adv-037, adversarial/adv-038, synthetic-core/core-002, synthetic-core/core-008, synthetic-core/core-020, synthetic-core/core-025, synthetic-core/core-030, synthetic-core/core-031, synthetic-core/core-046, synthetic-core/core-065, synthetic-core/core-068, synthetic-core/core-074, synthetic-core/core-076, synthetic-core/core-082, synthetic-core/core-084, synthetic-core/core-085, synthetic-core/core-086, synthetic-core/core-088, synthetic-core/core-091, synthetic-core/core-092, synthetic-core/core-094, synthetic-core/core-095, synthetic-core/core-101, synthetic-core/core-108, synthetic-core/core-109, synthetic-core/core-112, synthetic-core/core-117, synthetic-core/core-120

Root-cause breakdown of failures:

| Root cause | Count |
|---|---|
| ocr_misread | 6 |
| rule_false_negative | 126 |
| calibration_miss | 45 |
| other | 117 |

