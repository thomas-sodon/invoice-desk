"use client";

import { PhoneCall, ShieldCheck } from "lucide-react";
import * as React from "react";
import { useApi } from "@/components/api-provider";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import type { DocumentDetail } from "@/lib/api-types";
import { approvalKindFor, approvalProgress, approveGate, clearingKindFor, KIND_LABELS } from "@/lib/approvals";
import { maskFieldValue } from "@/lib/mask";
import { shortDate } from "@/lib/utils";

/**
 * Two-person approval with callback attestation (US-5). Approve controls are hidden from the
 * uploader and from clerks; the API enforces the same rules (403 self_approval) regardless.
 */
export function ApprovalPanel({ doc, onDone }: { doc: DocumentDetail; onDone: () => void }) {
  // Keyed by document: a ticked callback box or a note must never carry over to another invoice
  // when a queue reuses this panel for the next item.
  return <ApprovalPanelFor key={doc.id} doc={doc} onDone={onDone} />;
}

function ApprovalPanelFor({ doc, onDone }: { doc: DocumentDetail; onDone: () => void }) {
  const { api, me } = useApi();
  // An open over-limit / new-vendor problem on a held invoice is approved first (ADR-0008).
  const clearing = clearingKindFor(doc);
  const kind = clearing ?? approvalKindFor(doc);
  const gate = approveGate(doc, me);
  const progress = approvalProgress(doc);
  const callbackRequired = clearing ? false : (doc.approvals_required?.callback ?? kind === "bank_change");
  const [attested, setAttested] = React.useState(false);
  const [note, setNote] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const remit = doc.fields.find((f) => f.name === "remit_to_bank_details");

  if (!kind || (doc.status !== "Flagged" && !(doc.status === "NeedsReview" && (kind === "new_vendor" || kind === "over_cap"))))
    return null;

  const act = async (decision: "approve" | "reject") => {
    if (!api) return;
    setBusy(true);
    setError(null);
    try {
      await api.approve(doc.id, { kind, decision, callback_attested: attested, note: note || undefined });
      setAttested(false);
      setNote("");
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Approval" className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <ShieldCheck className="h-5 w-5 text-slate-700" />
        <span className="font-semibold">{KIND_LABELS[kind] ?? kind}</span>
        <span className="text-slate-600">
          {clearing ? (
            "· one approval"
          ) : (
            <>
              · {progress.have} of {progress.need} approval{progress.need === 1 ? "" : "s"}
              {callbackRequired && (progress.callback ? " · callback done" : " · callback needed")}
            </>
          )}
        </span>
      </div>
      {clearing && (
        <p className="text-slate-700" data-testid="clear-first">
          Approve the {clearing === "over_cap" ? "amount over the spending limit" : "new vendor"} first. The checks then
          run again, and the bank-change vote happens on the updated invoice.
        </p>
      )}
      {kind === "bank_change" && (
        <p className="text-slate-700">
          Bank changes always go to people: two approvers, neither the person who uploaded it, and a phone call to the
          vendor on the number already on file.
        </p>
      )}
      {remit?.display_value && (
        <p className="text-sm text-slate-700">
          Bank details on this invoice: <span className="font-mono">{maskFieldValue("remit_to_bank_details", remit.display_value)}</span>
        </p>
      )}
      {doc.approvals.length > 0 && (
        <ul className="space-y-1 text-sm">
          {doc.approvals.map((a, i) => (
            <li key={i} className="flex flex-wrap gap-x-2">
              <span className={a.decision === "approve" ? "font-semibold text-green-800" : "font-semibold text-red-800"}>
                {a.decision === "approve" ? "Approved" : "Rejected"}
              </span>
              <span>by {a.approver_name ?? `staff ${a.approver_id.slice(0, 8)}`}</span>
              {a.callback_attested && (
                <span className="inline-flex items-center gap-1 text-slate-600">
                  <PhoneCall className="h-3.5 w-3.5" /> called vendor
                </span>
              )}
              <span className="text-slate-500">{shortDate(a.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
      {progress.rejected && (
        <Alert tone="red" data-testid="approval-vetoed">
          An approver voted to reject this invoice, so it can&apos;t be released for payment. Reject it, or ask the vendor
          for a corrected invoice.
        </Alert>
      )}
      {gate.allowed ? (
        <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
          {callbackRequired && (
            <label className="flex items-start gap-3 text-base">
              <input
                type="checkbox"
                className="mt-1 h-5 w-5 shrink-0"
                checked={attested}
                onChange={(e) => setAttested(e.target.checked)}
              />
              <span>
                I called the vendor on the phone number <strong>already on file</strong> (not one printed on this
                invoice) and they confirmed it.
              </span>
            </label>
          )}
          <label className="block text-sm text-slate-700">
            Note (optional)
            <input
              className="mt-1 h-10 w-full rounded-md border border-slate-300 bg-white px-3"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
          <div className="flex flex-wrap gap-2">
            <Button variant="success" disabled={busy || (callbackRequired && !attested)} onClick={() => void act("approve")}>
              Approve
            </Button>
            <Button variant="outline" disabled={busy} onClick={() => void act("reject")}>
              Vote to reject
            </Button>
          </div>
        </div>
      ) : (
        <Alert tone="amber" data-testid="approve-blocked">
          {gate.message}
        </Alert>
      )}
      {error && <Alert tone="red">{error}</Alert>}
    </section>
  );
}
