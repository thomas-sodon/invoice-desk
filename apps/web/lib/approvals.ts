/**
 * Approval display logic. The server decides (two distinct approvers, neither the uploader, a
 * callback attestation); the UI only hides buttons that would certainly be refused and explains why.
 */
import type { ApprovalKind, DocumentDetail, Me, RuleResult } from "./api-types";

/**
 * Review-level failures that refuse a two-person release of a Flagged invoice (ADR-0008): the
 * approvers judge the hold (bank change, duplicate, look-alike, hidden instruction), never an
 * unverified total. These are fixed first: corrected, or cleared by an over-limit / new-vendor
 * approval, which re-runs the checks and opens a new hold.
 */
export function releaseBlockers(doc: Pick<DocumentDetail, "status" | "rule_results">): RuleResult[] {
  if (doc.status !== "Flagged") return [];
  return doc.rule_results.filter((r) => !r.passed && r.severity !== "flag");
}

/** The approval that clears an open R7 / R1 review failure on a Flagged invoice, if any. */
export function clearingKindFor(doc: Pick<DocumentDetail, "status" | "rule_results">): "over_cap" | "new_vendor" | null {
  const open = releaseBlockers(doc);
  if (open.some((r) => r.rule_id === "R7")) return "over_cap";
  if (open.some((r) => r.rule_id === "R1")) return "new_vendor";
  return null;
}

export function approvalKindFor(doc: Pick<DocumentDetail, "rule_results" | "badges" | "status" | "screen_results">): ApprovalKind | null {
  const failing = new Set(doc.rule_results.filter((r) => !r.passed).map((r) => r.rule_id));
  const categories = new Set(doc.screen_results.flatMap((s) => s.categories));
  if (failing.has("R6") || categories.has("bank_or_remit_change") || doc.badges.includes("bank_change")) return "bank_change";
  if (failing.has("R10") || doc.badges.includes("injection")) return "injection";
  if (failing.has("R4")) return "duplicate";
  if (doc.badges.includes("lookalike")) return "lookalike";
  if (failing.has("R7")) return "over_cap";
  if (failing.has("R1")) return "new_vendor";
  return null;
}

export type ApproveGate =
  | { allowed: true }
  | { allowed: false; reason: "not_waiting" | "clerk" | "uploader" | "already_approved" | "fix_first"; message: string };

export function approveGate(doc: Pick<DocumentDetail, "status" | "approvals" | "uploaded_by" | "rule_results" | "badges" | "screen_results">, me: Me | null): ApproveGate {
  const kind = approvalKindFor(doc);
  const waiting = doc.status === "Flagged" || (doc.status === "NeedsReview" && (kind === "new_vendor" || kind === "over_cap"));
  if (!waiting) return { allowed: false, reason: "not_waiting", message: "This invoice isn't waiting for an approval." };
  if (me && me.role === "clerk")
    return { allowed: false, reason: "clerk", message: "An approver or the owner must approve this. Clerks can reject it or ask the vendor." };
  if (me && doc.uploaded_by && me.id === doc.uploaded_by)
    return { allowed: false, reason: "uploader", message: "You uploaded this invoice, so someone else has to approve it." };
  const blockers = releaseBlockers(doc);
  if (blockers.length > 0 && clearingKindFor(doc) === null)
    return {
      allowed: false,
      reason: "fix_first",
      message: `Fix these before the release vote (correct the fields, or reject the invoice): ${blockers
        .map((r) => `${r.rule_id}: ${r.message}`)
        .join("; ")}`,
    };
  if (me && blockers.length === 0 && doc.approvals.some((a) => a.approver_id === me.id && a.decision === "approve"))
    return { allowed: false, reason: "already_approved", message: "You've approved this. A second person must approve it too." };
  return { allowed: true };
}

export function approvalProgress(doc: Pick<DocumentDetail, "approvals" | "approvals_required">): {
  have: number;
  need: number;
  callback: boolean;
  /** Any reject vote blocks release (invoicedesk.status._check_approvals). */
  rejected: boolean;
} {
  const need = doc.approvals_required?.count ?? 1;
  const have = new Set(doc.approvals.filter((a) => a.decision === "approve").map((a) => a.approver_id)).size;
  const callback = doc.approvals.some((a) => a.decision === "approve" && a.callback_attested);
  const rejected = doc.approvals.some((a) => a.decision === "reject");
  return { have, need, callback, rejected };
}

export const KIND_LABELS: Record<string, string> = {
  bank_change: "Bank-account change",
  injection: "Hidden instruction",
  duplicate: "Possible duplicate",
  lookalike: "Look-alike vendor",
  over_cap: "Over the spending limit",
  new_vendor: "New vendor",
};
