"use client";

import * as React from "react";
import { useApi } from "@/components/api-provider";
import type { AuditView } from "@/lib/api-types";
import { maskText } from "@/lib/mask";
import { shortDate } from "@/lib/utils";

function describe(after: unknown): string {
  if (after === null || after === undefined) return "";
  return maskText(JSON.stringify(after)).replace(/[{}"]/g, "").replace(/,/g, ", ").slice(0, 140);
}

export function AuditTrail({ documentId }: { documentId: string }) {
  const { api } = useApi();
  const [view, setView] = React.useState<AuditView | null>(null);
  const [loadError, setLoadError] = React.useState(false);
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => {
    if (!open || !api) return;
    // A failed request is not evidence of tampering: report it separately from chain_ok.
    api
      .getAudit(documentId)
      .then((v) => {
        setView(v);
        setLoadError(false);
      })
      .catch(() => setLoadError(true));
  }, [open, api, documentId]);
  return (
    <details className="group" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="cursor-pointer font-semibold text-slate-800">History (tamper-evident)</summary>
      {loadError && <p className="mt-2 text-sm text-slate-600">Couldn&apos;t load the history. Try again in a moment.</p>}
      {view && !loadError && (
        <div className="mt-2">
          <p className={view.chain_ok ? "text-sm text-green-800" : "text-sm text-red-800"}>
            {view.chain_ok ? "History verified: no entry has been altered." : "History check failed."}
          </p>
          <ol className="mt-2 max-h-72 space-y-1 overflow-y-auto text-sm">
            {view.entries.map((e) => (
              <li key={String(e.id)} className="grid grid-cols-[auto_1fr] gap-x-2">
                <span className="tabular-nums text-slate-500">{shortDate(e.ts)}</span>
                <span className="min-w-0 break-words">
                  <span className="font-medium">{e.action}</span> <span className="text-slate-500">by {e.actor}</span>{" "}
                  <span className="text-slate-600">{describe(e.after)}</span>
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </details>
  );
}
