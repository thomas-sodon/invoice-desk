"use client";

import { useApiData } from "@/components/api-provider";
import { Alert } from "@/components/ui/alert";
import { Card } from "@/components/ui/card";
import { errorMessage } from "@/lib/api";
import { formatCents, formatUsd } from "@/lib/money";

function Tile({ label, value, sub, tone = "slate" }: { label: string; value: string; sub?: string; tone?: "slate" | "green" | "red" }) {
  const color = tone === "green" ? "text-green-800" : tone === "red" ? "text-red-800" : "text-slate-900";
  return (
    <Card className="p-5">
      <p className="text-base font-medium text-slate-600">{label}</p>
      <p className={`mt-1 text-4xl font-extrabold tabular-nums ${color}`}>{value}</p>
      {sub && <p className="mt-1 text-slate-600">{sub}</p>}
    </Card>
  );
}

/** Owner dashboard (M12): dollars and hours, no jargon. */
export default function DashboardPage() {
  const { data, error } = useApiData((api) => api.dashboard(30));
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-3xl font-extrabold tracking-tight">Last 30 days</h1>
        <p className="text-lg text-slate-600">What the invoice desk did for Maple Row, in dollars and hours.</p>
      </div>
      {error ? <Alert tone="red">{errorMessage(error)}</Alert> : null}
      {data && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Tile label="Invoices handled" value={data.invoices.toLocaleString("en-US")} />
            <Tile label="Handled with no one touching them" value={`${data.hands_free_pct.toFixed(0)}%`} tone="green" />
            <Tile
              label="Hours saved"
              value={data.hours_saved.toLocaleString("en-US", { maximumFractionDigits: 1 })}
              sub={`at ${data.assumptions.minutes_per_invoice} minutes per invoice typed by hand`}
            />
            <Tile label="Money caught before it was paid" value={formatCents(data.dollars_caught_cents.total, { dropZeroCents: true })} tone="red" />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card className="p-5">
              <h2 className="text-xl font-bold">What was caught</h2>
              <dl className="mt-3 divide-y divide-slate-100 text-lg">
                {(
                  [
                    ["Duplicate bills", data.dollars_caught_cents.duplicates],
                    ["Fraud attempts (bank-account changes)", data.dollars_caught_cents.fraud],
                    ["Charges above the approved estimate", data.dollars_caught_cents.overcharges],
                  ] as const
                ).map(([label, cents]) => (
                  <div key={label} className="flex justify-between gap-3 py-2">
                    <dt className="text-slate-700">{label}</dt>
                    <dd className="font-bold tabular-nums">{formatCents(cents)}</dd>
                  </div>
                ))}
              </dl>
            </Card>
            <Card className="p-5">
              <h2 className="text-xl font-bold">Cost per invoice</h2>
              <p className="mt-3 text-4xl font-extrabold tabular-nums text-green-800">{formatUsd(data.cost_per_invoice_usd, 3)}</p>
              <p className="mt-1 text-lg text-slate-700">
                versus about {formatCents(data.manual_benchmark_per_invoice_cents)} to process one by hand (industry benchmark, for
                context).
              </p>
            </Card>
          </div>
          <p className="text-sm text-slate-500" data-testid="synthetic-label">
            Figures are a {data.label}. Hours saved assume {data.assumptions.minutes_per_invoice} minutes per invoice.
          </p>
        </>
      )}
    </div>
  );
}
