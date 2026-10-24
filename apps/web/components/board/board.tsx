"use client";

import { DndContext, KeyboardSensor, PointerSensor, useSensor, useSensors, type DragEndEvent } from "@dnd-kit/core";
import { Gauge, Play, Send } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import * as React from "react";
import { errorMessage } from "@/lib/api";
import type { DocStatus, DocumentCard, KanbanColumn } from "@/lib/api-types";
import { BOARD_COLUMNS, dropActionFor } from "@/lib/columns";
import { formatUsdMeter } from "@/lib/money";
import { splitUploads } from "@/lib/upload";
import { useApi } from "../api-provider";
import { Alert } from "../ui/alert";
import { Button } from "../ui/button";
import { Dialog } from "../ui/dialog";
import { useDocument } from "../use-document";
import { DocumentViewer, HiddenTextAlert } from "../viewer/document-viewer";
import { PageOverlay } from "../viewer/page-overlay";
import { Column, RejectTray } from "./column";
import { ScoreStrip } from "./score-strip";
import { UploadZone } from "./upload-zone";
import { useBoard } from "./use-board";

const LAST_BATCH_KEY = "invoicedesk.lastBatch";
/** `?batch=all` shows every invoice; without it the board opens on the latest batch. */
const ALL_BATCHES = "all";

function rememberBatch(id: string) {
  try {
    window.localStorage.setItem(LAST_BATCH_KEY, id);
  } catch {
    /* per-viewer convenience only */
  }
}

function DocPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const { doc, error, reload } = useDocument(id);
  return (
    <Dialog open onClose={onClose} title="Invoice">
      <div className="p-4 sm:p-6">
        {error ? <Alert tone="red">{errorMessage(error)}</Alert> : doc ? <DocumentViewer doc={doc} onChanged={reload} /> : <p>Loading…</p>}
        {doc && (
          <Link href={`/documents/${doc.id}`} className="mt-4 inline-block text-blue-700 underline">
            Open full page
          </Link>
        )}
      </div>
    </Dialog>
  );
}

function HeroPanel({ id }: { id: string }) {
  const { doc } = useDocument(id);
  if (!doc) return null;
  const page = doc.pages[0];
  return (
    <div className="absolute right-0 top-24 w-[37%] rounded-xl border border-slate-200 bg-white p-3 shadow-2xl" data-testid="hero-panel">
      <HiddenTextAlert doc={doc} compact />
      {page && (
        <div className="mt-2">
          <PageOverlay page={page} fields={doc.fields} screen={doc.screen_results} selected={null} alt={doc.filename} redactions={doc.redactions} />
        </div>
      )}
    </div>
  );
}

export function Board() {
  const { api } = useApi();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const urlBatch = params.get("batch");
  const openDoc = params.get("doc");
  // ?hero=1 renders the DESIGN §9 hero layout: three outcome columns plus a floating invoice panel.
  const hero = params.get("hero") === "1";
  const [batchId, setBatchId] = React.useState<string | null>(urlBatch === ALL_BATCHES ? null : urlBatch);
  const { cards, cost, loading, error, conn, reload } = useBoard(batchId);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<{ tone: "red" | "amber" | "green" | "blue"; text: string } | null>(null);

  // Default to the last batch this viewer worked on (or the mock's latest batch).
  React.useEffect(() => {
    if (urlBatch === ALL_BATCHES) {
      setBatchId(null);
      return;
    }
    if (urlBatch) {
      setBatchId(urlBatch);
      return;
    }
    if (!api) return;
    let last: string | null = null;
    try {
      last = window.localStorage.getItem(LAST_BATCH_KEY);
    } catch {
      last = null;
    }
    setBatchId(api.latestBatchId?.() ?? last);
  }, [api, urlBatch]);

  const setQuery = React.useCallback(
    (next: Record<string, string | null>) => {
      const q = new URLSearchParams(params.toString());
      for (const [k, v] of Object.entries(next)) {
        if (v === null) q.delete(k);
        else q.set(k, v);
      }
      const s = q.toString();
      router.replace(`${pathname}${s ? `?${s}` : ""}`, { scroll: false });
    },
    [params, pathname, router],
  );

  const selectBatch = (id: string) => {
    setBatchId(id);
    rememberBatch(id);
    setQuery({ batch: id, doc: null });
  };

  const byColumn = React.useMemo(() => {
    const m: Record<KanbanColumn, DocumentCard[]> = {
      received: [],
      extracted: [],
      validated: [],
      ready_to_pay: [],
      needs_review: [],
      flagged: [],
      posted: [],
      rejected: [],
    };
    for (const c of cards) m[c.column].push(c);
    return m;
  }, [cards]);

  const readyCount = byColumn.ready_to_pay.length;
  const inFlight = byColumn.received.length + byColumn.extracted.length + byColumn.validated.length;

  const runDemo = async () => {
    if (!api) return;
    setBusy("demo");
    setNotice(null);
    try {
      const r = await api.runDemo("replay");
      selectBatch(r.batch_id);
    } catch (e) {
      setNotice({ tone: "red", text: errorMessage(e) });
    } finally {
      setBusy(null);
    }
  };

  const upload = async (files: File[]) => {
    if (!api) return;
    const { ok, rejected } = splitUploads(files);
    if (ok.length === 0) {
      setNotice({ tone: "red", text: rejected.map((r) => r.error).join(" ") });
      return;
    }
    setBusy("upload");
    try {
      const r = await api.uploadDocuments(ok);
      selectBatch(r.batch_id);
      const allRejected = [...rejected, ...r.rejected];
      setNotice(
        allRejected.length
          ? { tone: "amber", text: `${r.documents.length} uploaded. Skipped: ${allRejected.map((x) => x.error).join(" ")}` }
          : { tone: "green", text: `${r.documents.length} invoice${r.documents.length === 1 ? "" : "s"} uploaded.` },
      );
    } catch (e) {
      setNotice({ tone: "red", text: errorMessage(e) });
    } finally {
      setBusy(null);
    }
  };

  const postReady = async () => {
    if (!api) return;
    setBusy("post");
    try {
      const r = batchId
        ? await api.postReady(batchId)
        : { count: (await Promise.all(byColumn.ready_to_pay.map((c) => api.post(c.id)))).length };
      setNotice({ tone: "green", text: `Posting ${r.count} bill${r.count === 1 ? "" : "s"} to QuickBooks Online with the original attached.` });
    } catch (e) {
      setNotice({ tone: "red", text: errorMessage(e) });
    } finally {
      setBusy(null);
    }
  };

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }), useSensor(KeyboardSensor));

  const onDragEnd = async (e: DragEndEvent) => {
    if (!api || !e.over) return;
    const status = (e.active.data.current as { status?: DocStatus } | undefined)?.status;
    if (!status) return;
    const id = String(e.active.id);
    const action = dropActionFor(status, e.over.id as KanbanColumn | "reject-tray");
    try {
      if (action.kind === "post") {
        await api.post(id);
        setNotice({ tone: "green", text: "Posting to QuickBooks Online…" });
      } else if (action.kind === "reject") {
        const reason = window.prompt("Why are you rejecting this invoice?");
        if (reason?.trim()) await api.reject(id, reason.trim());
      } else if (action.kind === "approve") {
        setQuery({ doc: id });
        setNotice({ tone: "amber", text: "Flagged invoices need two approvers and a callback. Use the approval panel." });
      } else if (action.reason) {
        setNotice({ tone: "amber", text: action.reason });
      }
    } catch (err) {
      setNotice({ tone: "red", text: errorMessage(err) });
    }
  };

  if (hero) {
    return (
      <div className="relative">
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <h1 className="text-3xl font-extrabold tracking-tight text-slate-900">Invoices</h1>
            <p className="text-base text-slate-600">25 messy invoices, checked in minutes.</p>
          </div>
          <ScoreStrip />
        </div>
        <DndContext sensors={sensors}>
          <div className="grid w-[62%] grid-cols-3 gap-2" data-testid="board">
            {(["ready_to_pay", "needs_review", "flagged"] as const).map((col) => (
              <Column key={col} column={col} cards={byColumn[col]} onOpen={() => undefined} dense />
            ))}
          </div>
        </DndContext>
        {openDoc && <HeroPanel id={openDoc} />}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between lg:block">
          <div>
            <h1 className="text-3xl font-extrabold tracking-tight text-slate-900">Invoices</h1>
            <p className="text-lg text-slate-600">Drop in a messy batch. Clean bills go to pay; anything risky waits for a person.</p>
          </div>
        </div>
        <div className="hidden 2xl:block">
          <ScoreStrip />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div
            className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2"
            aria-live="polite"
            data-testid="cost-meter"
          >
            <Gauge className="h-5 w-5 text-slate-500" aria-hidden />
            <span className="text-slate-600">{batchId ? "Batch cost" : "Cost shown"}</span>
            <span className="text-xl font-bold tabular-nums text-slate-900">{formatUsdMeter(cost)}</span>
            {inFlight > 0 && <span className="text-sm text-blue-700">· {inFlight} reading</span>}
          </div>
          <Button size="lg" onClick={() => void runDemo()} disabled={busy !== null} data-testid="run-demo">
            <Play className="h-5 w-5" /> Run 25-invoice demo
          </Button>
          <Button
            size="lg"
            variant="success"
            disabled={readyCount === 0 || busy !== null}
            onClick={() => void postReady()}
            data-testid="post-ready"
          >
            <Send className="h-5 w-5" /> Post {readyCount} to QuickBooks
          </Button>
        </div>
      </div>

      <UploadZone onFiles={(f) => void upload(f)} busy={busy === "upload"} compact={cards.length > 0} />

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-slate-600">
        <span>Works with QuickBooks Online.</span>
        {batchId && (
          <Link href={`/compare?batch=${batchId}`} className="font-semibold text-blue-700 underline">
            Basic AI vs InvoiceDesk on this batch
          </Link>
        )}
        {batchId && (
          <button type="button" className="text-blue-700 underline" onClick={() => { setBatchId(null); setQuery({ batch: ALL_BATCHES, doc: null }); }}>
            Show all invoices
          </button>
        )}
        {api?.mode === "http" && conn !== "live" && <span className="text-amber-700">Reconnecting to live updates…</span>}
      </div>

      {notice && (
        <Alert tone={notice.tone} className="flex items-start justify-between gap-3">
          <span>{notice.text}</span>
          <button type="button" className="text-sm underline" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </Alert>
      )}
      {error ? (
        <Alert tone="red">
          {errorMessage(error)}{" "}
          <button type="button" className="underline" onClick={() => void reload()}>
            Retry
          </button>
        </Alert>
      ) : null}

      <DndContext sensors={sensors} onDragEnd={(e) => void onDragEnd(e)}>
        <div
          className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-[repeat(3,minmax(0,0.75fr))_repeat(3,minmax(0,1.25fr))_minmax(0,0.9fr)]"
          data-testid="board"
          aria-busy={loading}
        >
          {BOARD_COLUMNS.map((col) => (
            <Column key={col} column={col} cards={byColumn[col]} onOpen={(id) => setQuery({ doc: id })} />
          ))}
        </div>
        <RejectTray count={byColumn.rejected.length} />
      </DndContext>

      {openDoc && <DocPanel id={openDoc} onClose={() => setQuery({ doc: null })} />}
    </div>
  );
}
