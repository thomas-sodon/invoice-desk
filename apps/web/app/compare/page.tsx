"use client";

import { Check, X } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { useApi, useApiData } from "@/components/api-provider";
import { Alert } from "@/components/ui/alert";
import { errorMessage } from "@/lib/api";
import type { ComparisonSide } from "@/lib/api-types";
import { formatCents } from "@/lib/money";
import { cn } from "@/lib/utils";

function Side({ side, who }: { side: ComparisonSide; who: "naive" | "ours" }) {
  const wrong = side.verdict === "wrong";
  return (
    <div className={cn("relative rounded-lg border p-3", wrong ? "border-red-300 bg-red-50" : "border-green-200 bg-green-50/60")}>
      <div className="flex items-start gap-2 pr-20">
        {wrong ? <X className="mt-0.5 h-5 w-5 shrink-0 text-red-700" /> : <Check className="mt-0.5 h-5 w-5 shrink-0 text-green-700" />}
        <span className={wrong ? "font-semibold text-red-900" : "text-green-900"}>{side.reason}</span>
      </div>
      {wrong ? (
        <span className="stamp absolute right-2 top-2 rounded px-1.5 py-0.5 text-sm font-black text-red-700" data-testid={`wrong-${who}`}>
          WRONG
        </span>
      ) : (
        <span className="absolute right-2 top-2 grid h-7 w-7 place-items-center rounded-full bg-green-700 text-white" aria-label="Correct">
          <Check className="h-4 w-4" />
        </span>
      )}
    </div>
  );
}

function Comparison() {
  const { api } = useApi();
  const params = useSearchParams();
  const batch = params.get("batch") ?? api?.latestBatchId?.() ?? null;
  const { data, error, loading } = useApiData(
    async (a) => (batch ? a.comparison(batch) : Promise.reject(new Error("Pick a batch on the board first."))),
    [batch],
  );
  const onlyDiff = params.get("all") !== "1";
  const rows = data ? (onlyDiff ? data.rows.filter((r) => r.naive.verdict !== r.invoicedesk.verdict) : data.rows) : [];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-3xl font-extrabold tracking-tight">Basic AI vs InvoiceDesk</h1>
        <p className="text-lg text-slate-600">
          The same batch through a basic &ldquo;read it and post it&rdquo; script, and through InvoiceDesk&apos;s checks.
        </p>
      </div>
      {error && !loading ? <Alert tone="amber">{errorMessage(error)}</Alert> : null}
      {data && (
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="rounded-xl border-2 border-red-300 bg-white p-5">
              <p className="text-lg font-semibold text-slate-700">Basic AI</p>
              <p className="text-4xl font-extrabold text-red-700">{data.summary.naive_wrong} wrong</p>
              <p className="text-lg text-slate-700">
                would have paid {formatCents(data.summary.naive_dollars_lost_cents)} it shouldn&apos;t have
              </p>
            </div>
            <div className="rounded-xl border-2 border-green-300 bg-white p-5">
              <p className="text-lg font-semibold text-slate-700">InvoiceDesk</p>
              <p className="text-4xl font-extrabold text-green-800">{data.summary.invoicedesk_wrong} wrong</p>
              <p className="text-lg text-slate-700">held every risky invoice for a person</p>
            </div>
          </div>
          <div className="flex flex-wrap gap-3 text-base">
            <Link className="text-blue-700 underline" href={`/compare?batch=${batch}${onlyDiff ? "&all=1" : ""}`}>
              {onlyDiff ? `Show all ${data.rows.length} invoices` : "Show only where they differ"}
            </Link>
          </div>
          <div className="hidden grid-cols-[minmax(0,14rem)_1fr_1fr] gap-3 px-1 text-sm font-semibold uppercase tracking-wide text-slate-500 md:grid">
            <span>Invoice</span>
            <span>Basic AI</span>
            <span>InvoiceDesk</span>
          </div>
          <ul className="space-y-3" data-testid="comparison-rows">
            {rows.map((r) => (
              <li key={r.document_id} className="grid gap-2 rounded-xl border border-slate-200 bg-white p-3 md:grid-cols-[minmax(0,14rem)_1fr_1fr] md:items-center md:gap-3">
                <Link href={`/documents/${r.document_id}`} className="break-all font-medium text-blue-800 underline">
                  {r.filename}
                </Link>
                <Side side={r.naive} who="naive" />
                <Side side={r.invoicedesk} who="ours" />
              </li>
            ))}
          </ul>
          <p className="text-sm text-slate-500">Dry run on synthetic invoices; the basic script never touches the real books.</p>
        </>
      )}
    </div>
  );
}

export default function ComparePage() {
  return (
    <Suspense fallback={<p>Loading…</p>}>
      <Comparison />
    </Suspense>
  );
}
