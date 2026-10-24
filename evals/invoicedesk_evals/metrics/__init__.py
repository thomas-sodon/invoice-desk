"""Eval metrics (DESIGN §5.6, M17): fields, line items, STP/rules/guardrails, calibration,
boxes, cost and latency. Every metric is a pure function of per-document scores, so the same
code computes point estimates and bootstrap resamples."""
