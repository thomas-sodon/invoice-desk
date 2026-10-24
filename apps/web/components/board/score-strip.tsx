"use client";

import type { Scorecard } from "@/lib/api-types";
import { formatMetric } from "@/lib/metrics";
import { useApiData } from "../api-provider";

const TILES: { key: string; label: string; suffix?: string }[] = [
  { key: "field_f1", label: "Field accuracy" },
  { key: "stp", label: "Hands-free" },
  { key: "usd_per_doc", label: "per invoice" },
  { key: "p50_s", label: "per invoice" },
];

/** Four-tile strip from the latest eval run (DESIGN §9 hero). Hidden until a run exists. */
export function ScoreStrip() {
  const { data } = useApiData<Scorecard | null>((api) => api.scorecard());
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
      <span className="text-xs text-slate-500">{data.label === "benchmark on synthetic data" ? "benchmark, synthetic data" : data.label}</span>
    </div>
  );
}
