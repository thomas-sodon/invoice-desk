/** Scorecard metric labels and formatting. Rates are fractions (0..1) in the eval store. */
export const METRIC_ORDER = [
  "field_f1",
  "stp",
  "usd_per_doc",
  "p50_s",
  "false_stp",
  "line_item_f1",
  "injection_recall",
  "ece",
  "p95_s",
] as const;

const LABELS: Record<string, string> = {
  field_f1: "Field accuracy (F1)",
  line_item_f1: "Line-item accuracy (F1)",
  stp: "Hands-free rate",
  false_stp: "Wrongly hands-free",
  injection_recall: "Hidden-instruction catch rate",
  injection_fpr: "False alarms on clean invoices",
  ece: "Calibration error",
  usd_per_doc: "Cost per invoice",
  p50_s: "Median time per invoice",
  p95_s: "95th-percentile time",
};

const PERCENT = new Set(["field_f1", "line_item_f1", "stp", "false_stp", "injection_recall", "injection_fpr"]);

export function metricLabel(key: string): string {
  return LABELS[key] ?? key.replace(/_/g, " ");
}

export function formatMetric(key: string, value: number | string): string {
  if (key === "usd_per_doc") {
    const s = typeof value === "number" ? String(value) : value;
    return `$${s}`;
  }
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (PERCENT.has(key)) return `${(Math.round(n * 1000) / 10).toFixed(1).replace(/\.0$/, "")}%`;
  if (key.endsWith("_s")) return `${n.toFixed(1)} s`;
  if (key === "ece") return n.toFixed(3);
  return String(n);
}
