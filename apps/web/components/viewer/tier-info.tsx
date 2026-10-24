import type { DocumentDetail } from "@/lib/api-types";
import { formatUsd } from "@/lib/money";

const TIER_NAME: Record<number, string> = { 0: "Safety check", 1: "Fast reader", 2: "Careful reader", 3: "Expert review" };

export function TierInfo({ doc }: { doc: DocumentDetail }) {
  if (doc.extractions.length === 0) return <p className="text-slate-600">No reading cost yet.</p>;
  return (
    <div>
      <table className="w-full text-left text-sm">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 font-medium">Step</th>
            <th className="py-1 font-medium">Model</th>
            <th className="py-1 text-right font-medium">Time</th>
            <th className="py-1 text-right font-medium">Cost</th>
          </tr>
        </thead>
        <tbody>
          {doc.extractions.map((e, i) => (
            <tr key={i} className="border-t border-slate-100">
              <td className="py-1.5">
                Tier {e.tier} · {TIER_NAME[e.tier] ?? "Reader"}
                {e.refused && <span className="ml-1 text-amber-700">(declined, escalated)</span>}
              </td>
              <td className="py-1.5 break-all text-slate-600">{e.model_id}</td>
              <td className="py-1.5 text-right tabular-nums">{(e.latency_ms / 1000).toFixed(1)} s</td>
              <td className="py-1.5 text-right tabular-nums">{formatUsd(e.cost_usd, 4)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t border-slate-200 font-semibold">
            <td className="py-1.5" colSpan={3}>
              Total for this invoice
            </td>
            <td className="py-1.5 text-right tabular-nums">{formatUsd(doc.cost_usd, 4)}</td>
          </tr>
        </tfoot>
      </table>
      {doc.trace_url && (
        <a className="mt-2 inline-block text-sm text-blue-700 underline" href={doc.trace_url} target="_blank" rel="noreferrer">
          Open trace
        </a>
      )}
    </div>
  );
}
