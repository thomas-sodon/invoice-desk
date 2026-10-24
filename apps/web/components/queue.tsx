"use client";

import * as React from "react";
import type { ApiClient } from "@/lib/api";
import { errorMessage } from "@/lib/api";
import type { DocumentCard, DocumentDetail } from "@/lib/api-types";
import { badgeInfo } from "@/lib/badges";
import { maskText } from "@/lib/mask";
import { formatCents } from "@/lib/money";
import { cn } from "@/lib/utils";
import { useApi } from "./api-provider";
import { Alert } from "./ui/alert";
import { Chip } from "./ui/chip";
import { useDocument } from "./use-document";

/** Master-detail layout shared by the review queue and the approvals queue. */
export function Queue({
  title,
  intro,
  load,
  empty,
  renderDetail,
}: {
  title: string;
  intro: string;
  load: (api: ApiClient) => Promise<DocumentCard[]>;
  empty: string;
  renderDetail: (doc: DocumentDetail, done: () => void) => React.ReactNode;
}) {
  const { api } = useApi();
  const [items, setItems] = React.useState<DocumentCard[] | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const [selected, setSelected] = React.useState<string | null>(null);
  const loadRef = React.useRef(load);
  loadRef.current = load;

  const refresh = React.useCallback(async () => {
    if (!api) return;
    try {
      const list = await loadRef.current(api);
      setItems(list);
      setSelected((cur) => (cur && list.some((d) => d.id === cur) ? cur : (list[0]?.id ?? null)));
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, [api]);

  React.useEffect(() => {
    void refresh();
    if (!api) return;
    const sub = api.subscribe(null, (e) => {
      if (e.type === "document.status_changed" || e.type === "document.created") void refresh();
    });
    return () => sub.close();
  }, [api, refresh]);

  const { doc, reload } = useDocument(selected);
  const done = () => {
    reload();
    void refresh();
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-3xl font-extrabold tracking-tight">{title}</h1>
        <p className="text-lg text-slate-600">{intro}</p>
      </div>
      {error ? <Alert tone="red">{errorMessage(error)}</Alert> : null}
      {items && items.length === 0 && <Alert tone="green">{empty}</Alert>}
      {items && items.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-[20rem_minmax(0,1fr)]">
          <ul className="space-y-2" aria-label={`${title} list`}>
            {items.map((d) => (
              <li key={d.id}>
                <button
                  type="button"
                  onClick={() => setSelected(d.id)}
                  aria-current={selected === d.id}
                  className={cn(
                    "w-full rounded-lg border bg-white p-3 text-left shadow-sm",
                    selected === d.id ? "border-blue-600 ring-2 ring-blue-200" : "border-slate-200 hover:border-slate-300",
                  )}
                >
                  <span className="flex justify-between gap-2">
                    <span className="truncate font-semibold">{d.vendor_name ?? d.filename}</span>
                    <span className="shrink-0 tabular-nums">{formatCents(d.total_cents)}</span>
                  </span>
                  {d.headline && <span className="mt-1 block text-[0.95rem] text-slate-700">{maskText(d.headline)}</span>}
                  <span className="mt-1 flex flex-wrap gap-1">
                    {d.badges.slice(0, 3).map((b) => (
                      <Chip key={b} tone={badgeInfo(b).tone} className="text-xs">
                        {badgeInfo(b).label}
                      </Chip>
                    ))}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <div className="min-w-0 rounded-xl border border-slate-200 bg-white p-4 sm:p-5">
            {doc ? renderDetail(doc, done) : <p className="text-slate-600">Select an invoice.</p>}
          </div>
        </div>
      )}
    </div>
  );
}
