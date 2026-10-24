/** Kanban column mapping: mirrors invoicedesk.status.KANBAN_COLUMNS (DESIGN §4.3). */
import type { DocStatus, KanbanColumn } from "./api-types";

export const STATUS_TO_COLUMN: Record<DocStatus, KanbanColumn> = {
  Received: "received",
  Screening: "received",
  Extracting: "received",
  Extracted: "extracted",
  Validating: "validated",
  ReadyToPay: "ready_to_pay",
  NeedsReview: "needs_review",
  Flagged: "flagged",
  Quarantined: "flagged",
  Posted: "posted",
  Rejected: "rejected",
};

export function columnFor(status: DocStatus): KanbanColumn {
  return STATUS_TO_COLUMN[status];
}

/** Board order; "rejected" is shown as a collapsed tray under the board, not a column. */
export const BOARD_COLUMNS: readonly KanbanColumn[] = [
  "received",
  "extracted",
  "validated",
  "ready_to_pay",
  "needs_review",
  "flagged",
  "posted",
];

export const COLUMN_LABELS: Record<KanbanColumn, string> = {
  received: "Received",
  extracted: "Extracted",
  validated: "Validated",
  ready_to_pay: "Ready to pay",
  needs_review: "Needs review",
  flagged: "Flagged",
  posted: "Posted",
  rejected: "Rejected",
};

export type Tone = "neutral" | "green" | "amber" | "red" | "blue";

export const COLUMN_TONE: Record<KanbanColumn, Tone> = {
  received: "neutral",
  extracted: "neutral",
  validated: "blue",
  ready_to_pay: "green",
  needs_review: "amber",
  flagged: "red",
  posted: "green",
  rejected: "neutral",
};

export const STATUS_LABELS: Record<DocStatus, string> = {
  Received: "Received",
  Screening: "Checking for tampering",
  Quarantined: "Quarantined",
  Extracting: "Reading",
  Extracted: "Read",
  Validating: "Checking",
  ReadyToPay: "Ready to pay",
  NeedsReview: "Needs review",
  Flagged: "Flagged",
  Rejected: "Rejected",
  Posted: "Posted",
};

/**
 * Human moves the board allows by drag. Everything else is decided by the checks: dropping a card
 * elsewhere is refused. These map to API calls, which re-check the state machine server-side.
 */
export type DropAction = { kind: "post" } | { kind: "reject" } | { kind: "approve" } | { kind: "refuse"; reason: string };

export function dropActionFor(status: DocStatus, target: KanbanColumn | "reject-tray"): DropAction {
  if (target === "posted" && status === "ReadyToPay") return { kind: "post" };
  if (target === "reject-tray" && (status === "NeedsReview" || status === "Flagged")) return { kind: "reject" };
  if (target === "ready_to_pay" && status === "Flagged") return { kind: "approve" };
  if (target === "ready_to_pay" && status === "NeedsReview")
    return { kind: "refuse", reason: "Fix the highlighted fields first; the checks then decide." };
  if (columnFor(status) === target) return { kind: "refuse", reason: "" };
  return { kind: "refuse", reason: "Only the checks move invoices between these columns." };
}
