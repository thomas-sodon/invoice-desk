"use client";

import type { Scorecard } from "@/lib/api-types";
import { formatMetric } from "@/lib/metrics";
import { useApi, useApiData } from "../api-provider";

/** The caption under the tiles. Mock-mode numbers are samples and offline-harness runs are not
 * model measurements, so neither may read as a benchmark (CLAUDE.md: no over-claiming). */
export function scoreCaption(label: string, mode: string | undefined): string {
  if (mode === "mock") return "sample values, not a measured run";
  if (label.includes("offline harness")) return "offline harness run, not a measurement";
  return label === "benchmark on synthetic data" ? "benchmark, synthetic data" : label;
}

const TILES: { key: string; label: string; suffix?: string }[] = [
  { key: "field_f1", label: "Field accuracy" },
  { key: "stp", label: "Hands-free" },
  { key: "usd_per_doc", label: "per invoice" },
  { key: "p50_s", label: "per invoice" },
];

/** Four-tile strip from the latest eval run (DESIGN §9 hero). Hidden until a run exists. */
export function ScoreStrip() {
  const { api } = useApi();
  const { data } = useApiData<Scorecard | null>((client) => client.scorecard());
  if (!data) return null;
  const tiles = TILES.filter((t) => data.metrics[t.key]);
  if (tiles.length === 0) return null;
  return (
    <div className="flex flex-col items-end gap-0.5" data-testid="score-strip">
      <div className="flex flex-wrap justify-end gap-1.5">
        {tiles.map((t) => {
          const m = data.metrics[t.key];
          if (!m) return null;
          return (
            <span key={t.key} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-center leading-tight">
              <span className="block text-xl font-extrabold tabular-nums text-slate-900">{formatMetric(t.key, m.value)}</span>
              <span className="block text-xs text-slate-600">{t.label}</span>
            </span>
          );
        })}
      </div>
      <span className="text-xs text-slate-500" data-testid="score-caption">{scoreCaption(data.label, api?.mode)}</span>
    </div>
  );
}
