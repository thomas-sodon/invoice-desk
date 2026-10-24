"use client";

import { FileDown } from "lucide-react";
import { useApi, useApiData } from "@/components/api-provider";
import { Alert } from "@/components/ui/alert";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { errorMessage } from "@/lib/api";
import type { ReliabilityBin } from "@/lib/api-types";
import { formatMetric, METRIC_ORDER, metricLabel } from "@/lib/metrics";
import { shortDate } from "@/lib/utils";

function ReliabilityDiagram({ bins }: { bins: ReliabilityBin[] }) {
  const W = 320;
  const pad = 48;
  const inner = W - pad * 2;
  const maxCount = Math.max(1, ...bins.map((b) => b.count));
  const x = (v: number) => pad + v * inner;
  const y = (v: number) => W - pad - v * inner;
  return (
    <svg viewBox={`0 0 ${W} ${W}`} className="h-auto w-full max-w-sm" role="img" aria-label="Reliability diagram: stated confidence against observed accuracy">
      <rect x={pad} y={pad} width={inner} height={inner} fill="#fff" stroke="#cbd5e1" />
      {[0.25, 0.5, 0.75].map((t) => (
        <g key={t}>
          <line x1={x(t)} y1={y(0)} x2={x(t)} y2={y(1)} stroke="#f1f5f9" />
          <line x1={x(0)} y1={y(t)} x2={x(1)} y2={y(t)} stroke="#f1f5f9" />
        </g>
      ))}
      <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} stroke="#94a3b8" strokeDasharray="4 4" />
      {bins.map((b, i) => (
        <circle key={i} cx={x(b.confidence)} cy={y(b.accuracy)} r={3 + 6 * Math.sqrt(b.count / maxCount)} fill="#1d4ed8" fillOpacity={0.75}>
          <title>{`confidence ${Math.round(b.confidence * 100)}%, accuracy ${Math.round(b.accuracy * 100)}%, ${b.count} fields`}</title>
        </circle>
      ))}
      <polyline
        fill="none"
        stroke="#1d4ed8"
        strokeWidth={2}
        points={[...bins].sort((a, b) => a.confidence - b.confidence).map((b) => `${x(b.confidence)},${y(b.accuracy)}`).join(" ")}
      />
      <text x={W / 2} y={W - 6} textAnchor="middle" fontSize="12" fill="#475569">
        Stated confidence
      </text>
      <text x={12} y={W / 2} textAnchor="middle" fontSize="12" fill="#475569" transform={`rotate(-90 12 ${W / 2})`}>
        Observed accuracy
      </text>
      {[0, 0.5, 1].map((t) => (
        <g key={t} fontSize="11" fill="#64748b">
          <text x={x(t)} y={y(0) + 14} textAnchor="middle">{`${t * 100}%`}</text>
          <text x={x(0) - 4} y={y(t) + 4} textAnchor="end">{`${t * 100}%`}</text>
        </g>
      ))}
    </svg>
  );
}

export default function ScorecardPage() {
  const { api } = useApi();
  const { data, error, loading } = useApiData((a) => a.scorecard());
  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-3xl font-extrabold tracking-tight">Scorecard</h1>
          <p className="text-lg text-slate-600">Measured on held-out test invoices, with 95% confidence intervals.</p>
        </div>
        {api && (
          <a href={api.reportCardUrl()} className={buttonVariants({ variant: "outline", size: "lg" })} target="_blank" rel="noreferrer">
            <FileDown className="h-5 w-5" /> Download the Report Card (PDF)
          </a>
        )}
      </div>
      {error ? <Alert tone="red">{errorMessage(error)}</Alert> : null}
      {!loading && !error && !data && <Alert tone="amber">No evaluation has been run yet, so there are no numbers to show.</Alert>}
      {api?.mode === "mock" && data && (
        <Alert tone="amber">Offline demo: these are sample values, not a measured run. Connect the server to load the latest eval run.</Alert>
      )}
      {data && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {METRIC_ORDER.filter((k) => data.metrics[k]).map((k) => {
              const m = data.metrics[k];
              if (!m) return null;
              return (
                <Card key={k} className="p-5" data-testid={`metric-${k}`}>
                  <p className="font-medium text-slate-600">{metricLabel(k)}</p>
                  <p className="mt-1 text-3xl font-extrabold tabular-nums">{formatMetric(k, m.value)}</p>
                  {m.ci && (
                    <p className="text-slate-600 tabular-nums">
                      95% CI {formatMetric(k, m.ci[0])} – {formatMetric(k, m.ci[1])}
                    </p>
                  )}
                </Card>
              );
            })}
          </div>
          {data.reliability && data.reliability.length > 0 && (
            <Card className="p-5">
              <h2 className="text-xl font-bold">Is the confidence honest?</h2>
              <p className="mb-3 text-slate-700">Each dot is a group of fields. On the dashed line, &ldquo;90% sure&rdquo; means right 90% of the time.</p>
              <ReliabilityDiagram bins={data.reliability} />
            </Card>
          )}
          <p className="text-sm text-slate-500" data-testid="synthetic-label">
            {data.label[0]?.toUpperCase()}
            {data.label.slice(1)} · run {data.run_id.slice(0, 8)} · config {data.config} · {data.git_sha.slice(0, 7)} · {shortDate(data.finished_at)}
          </p>
        </>
      )}
    </div>
  );
}
