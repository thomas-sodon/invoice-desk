"use client";

import { OctagonX } from "lucide-react";
import * as React from "react";
import { useApi } from "@/components/api-provider";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { errorMessage } from "@/lib/api";
import type { DocumentDetail } from "@/lib/api-types";
import { badgeInfo } from "@/lib/badges";
import { BAND_COLORS } from "@/lib/bands";
import { COLUMN_TONE, STATUS_LABELS } from "@/lib/columns";
import { formatCents } from "@/lib/money";
import { maskText } from "@/lib/mask";
import { hiddenTextSummary } from "@/lib/screening";
import { isPdfName } from "@/lib/upload";
import { ApprovalPanel } from "./approval-panel";
import { AuditTrail } from "./audit-trail";
import { CorrectionForm } from "./correction-form";
import { FieldsList } from "./fields-list";
import { FollowupBox } from "./followup-box";
import { PageOverlay } from "./page-overlay";
import { PdfPage } from "./pdf-page";
import { RulesList } from "./rules-list";
import { TierInfo } from "./tier-info";

function Section({ title, children, id }: { title: string; children: React.ReactNode; id?: string }) {
  return (
    <section id={id} className="border-t border-slate-200 pt-4">
      <h3 className="mb-2 text-lg font-bold text-slate-900">{title}</h3>
      {children}
    </section>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-slate-600" aria-label="Box colors">
      {(["green", "amber", "red"] as const).map((b) => (
        <span key={b} className="inline-flex items-center gap-1.5">
          <span className="inline-block h-3 w-4 rounded-sm border-2" style={{ borderColor: BAND_COLORS[b].stroke, background: BAND_COLORS[b].fill }} />
          {BAND_COLORS[b].label}
        </span>
      ))}
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-3 w-4 rounded-sm border-2 border-red-600" /> Revealed hidden text
      </span>
    </div>
  );
}

export function HiddenTextAlert({ doc, compact = false }: { doc: DocumentDetail; compact?: boolean }) {
  const summary = hiddenTextSummary(doc.screen_results);
  if (!summary) return null;
  if (compact)
    return (
      <p className="flex items-start gap-2 rounded-lg border-2 border-red-600 bg-red-50 p-2 text-base font-bold leading-snug text-red-800" role="alert" data-testid="hidden-text-alert">
        <OctagonX className="mt-0.5 h-5 w-5 shrink-0" />
        {summary.title}
      </p>
    );
  return (
    <div className="rounded-xl border-2 border-red-600 bg-red-50 p-4" role="alert" data-testid="hidden-text-alert">
      <p className="flex items-start gap-2 text-lg font-bold text-red-800">
        <OctagonX className="mt-1 h-6 w-6 shrink-0" />
        {summary.title}
      </p>
      <p className="mt-1 text-red-900">{summary.how}</p>
      {summary.spans.length > 0 && (
        <blockquote className="mt-2 border-l-4 border-red-400 pl-3 font-mono text-sm text-red-950">
          {summary.spans.map((s, i) => (
            <span key={i} className="block break-words">
              &ldquo;{s.text}&rdquo;
            </span>
          ))}
        </blockquote>
      )}
      <p className="mt-2 text-sm text-red-900">
        {doc.fields.length === 0 ? "The invoice was quarantined before any reading. " : "The hidden text was flagged before reading. "}
        {summary.bankChange ? "Bank changes always go to a person." : "A person decides what happens next."}
      </p>
    </div>
  );
}

function DocActions({ doc, onDone, allowPost }: { doc: DocumentDetail; onDone: () => void; allowPost: boolean }) {
  const { api } = useApi();
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [reason, setReason] = React.useState("");
  const [rejecting, setRejecting] = React.useState(false);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const canReject = doc.status === "NeedsReview" || doc.status === "Flagged";
  if (!api) return null;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {allowPost && doc.status === "ReadyToPay" && (
          <Button variant="success" disabled={busy} onClick={() => void run(() => api.post(doc.id))}>
            Post to QuickBooks
          </Button>
        )}
        {canReject && !rejecting && (
          <Button variant="outline" disabled={busy} onClick={() => setRejecting(true)}>
            Reject invoice
          </Button>
        )}
      </div>
      {rejecting && (
        <div className="flex flex-wrap gap-2">
          <input
            aria-label="Reason for rejecting"
            placeholder="Reason (e.g. duplicate of INV-2231)"
            className="h-11 min-w-0 flex-1 rounded-md border border-slate-300 px-3"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <Button variant="danger" disabled={busy || !reason.trim()} onClick={() => void run(() => api.reject(doc.id, reason.trim()))}>
            Confirm reject
          </Button>
          <Button variant="ghost" onClick={() => setRejecting(false)}>
            Cancel
          </Button>
        </div>
      )}
      {doc.bill && (
        <p className="text-green-800">
          Posted to QuickBooks Online as bill {doc.bill.external_id ?? "(pending)"} with the original attached.
        </p>
      )}
      {error && <Alert tone="red">{error}</Alert>}
    </div>
  );
}

export function DocumentViewer({
  doc,
  onChanged,
  variant = "staff",
}: {
  doc: DocumentDetail;
  onChanged: () => void;
  /** "visitor" hides approvals, posting, follow-ups and history (try-it page). */
  variant?: "staff" | "visitor";
}) {
  const [selected, setSelected] = React.useState<string | null>(null);
  const [showFields, setShowFields] = React.useState(true);
  const page = doc.pages[0];
  const staff = variant === "staff";
  const columnTone = COLUMN_TONE[doc.column];
  const pdfOriginal = !page && doc.original_url && isPdfName(doc.filename) ? doc.original_url : null;

  return (
    <article className="grid gap-5 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]" data-testid="document-viewer">
      <div className="space-y-3 lg:sticky lg:top-24 lg:self-start">
        {page ? (
          <PageOverlay
            page={page}
            fields={doc.fields}
            screen={doc.screen_results}
            selected={selected}
            showFields={showFields}
            onSelect={setSelected}
            redactions={doc.redactions}
            alt={`Invoice ${doc.filename}`}
          />
        ) : pdfOriginal ? (
          <PdfPage url={pdfOriginal} redactions={doc.redactions} fields={doc.fields} screen={doc.screen_results} selected={selected} onSelect={setSelected} />
        ) : (
          <div className="grid aspect-[3/4] place-items-center rounded-lg border border-dashed border-slate-300 bg-white text-slate-500">
            Page image not available yet
          </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Legend />
          <label className="inline-flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" checked={showFields} onChange={(e) => setShowFields(e.target.checked)} /> Show field boxes
          </label>
        </div>
      </div>

      <div className="min-w-0 space-y-4">
        <header className="space-y-2 pr-10">
          <div className="flex flex-wrap items-center gap-2">
            <Chip tone={columnTone}>{STATUS_LABELS[doc.status]}</Chip>
            {doc.badges.map((b) => {
              const info = badgeInfo(b);
              return (
                <Chip key={b} tone={info.tone}>
                  {info.label}
                </Chip>
              );
            })}
          </div>
          <h2 className="break-words text-2xl font-extrabold text-slate-900">{doc.vendor_name ?? doc.filename}</h2>
          <p className="text-lg text-slate-700">
            {doc.invoice_number ? `Invoice ${doc.invoice_number} · ` : ""}
            <span className="font-semibold tabular-nums">{formatCents(doc.total_cents)}</span>
          </p>
          <p className="break-all text-sm text-slate-500">{doc.filename}</p>
          {doc.headline && !hiddenTextSummary(doc.screen_results) && (
            <Alert tone={doc.status === "Flagged" ? "red" : doc.status === "NeedsReview" ? "amber" : "green"} className="text-lg font-semibold">
              {maskText(doc.headline)}
            </Alert>
          )}
        </header>

        <HiddenTextAlert doc={doc} />

        {staff && doc.status === "NeedsReview" && (
          <Section title="Fix and re-check" id="review">
            <CorrectionForm doc={doc} onDone={onChanged} />
          </Section>
        )}

        {staff && (
          <ApprovalSection doc={doc} onChanged={onChanged} />
        )}

        <Section title="What the checks found">
          <RulesList rules={doc.rule_results} />
        </Section>

        <Section title="What it read">
          <FieldsList fields={doc.fields} selected={selected} onSelect={setSelected} />
          {doc.line_items.length > 0 && (
            <table className="mt-3 w-full text-left text-sm">
              <thead className="text-slate-500">
                <tr>
                  <th className="py-1 font-medium">Line item</th>
                  <th className="py-1 text-right font-medium">Qty</th>
                  <th className="py-1 text-right font-medium">Amount</th>
                </tr>
              </thead>
              <tbody>
                {doc.line_items.map((li, i) => (
                  <tr key={i} className="border-t border-slate-100">
                    <td className="py-1.5 pr-2">{li.description ? maskText(li.description) : "—"}</td>
                    <td className="py-1.5 text-right tabular-nums">{li.quantity ?? "—"}</td>
                    <td className="py-1.5 text-right tabular-nums">{formatCents(li.amount_cents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>

        {staff && (doc.status === "NeedsReview" || doc.status === "Flagged" || doc.status === "ReadyToPay" || doc.bill) && (
          <Section title="Actions">
            <div className="space-y-3">
              <DocActions doc={doc} onDone={onChanged} allowPost />
              <FollowupBox doc={doc} />
            </div>
          </Section>
        )}

        <Section title="Reading cost">
          <TierInfo doc={doc} />
        </Section>

        {staff && (
          <section className="border-t border-slate-200 pt-4">
            <AuditTrail documentId={doc.id} />
          </section>
        )}
      </div>
    </article>
  );
}

function ApprovalSection({ doc, onChanged }: { doc: DocumentDetail; onChanged: () => void }) {
  const show = doc.status === "Flagged" || (doc.status === "NeedsReview" && doc.badges.some((b) => b === "new_vendor" || b === "over_cap"));
  if (!show) return null;
  return (
    <Section title="Approval" id="approval">
      <ApprovalPanel doc={doc} onDone={onChanged} />
    </Section>
  );
}
