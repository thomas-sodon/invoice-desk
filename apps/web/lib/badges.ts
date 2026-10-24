import type { Badge } from "./api-types";
import type { Tone } from "./columns";

export const BADGE_INFO: Record<Badge, { label: string; tone: Tone }> = {
  duplicate: { label: "Duplicate", tone: "red" },
  bank_change: { label: "Bank change", tone: "red" },
  hidden_text: { label: "Hidden text", tone: "red" },
  faint_text: { label: "Faint text", tone: "red" },
  injection: { label: "Hidden instruction", tone: "red" },
  over_cap: { label: "Over limit", tone: "amber" },
  new_vendor: { label: "New vendor", tone: "amber" },
  lookalike: { label: "Look-alike vendor", tone: "red" },
  low_confidence: { label: "Unclear field", tone: "amber" },
  handwriting: { label: "Handwritten", tone: "neutral" },
  over_estimate: { label: "Over estimate", tone: "amber" },
  arithmetic: { label: "Math doesn't add up", tone: "amber" },
  date: { label: "Date issue", tone: "amber" },
};

/** Badges the rules engine emits beyond the contract's documented list. */
const EXTRA_BADGES: Record<string, { label: string; tone: Tone }> = {
  work_order: { label: "Work order issue", tone: "amber" },
  blocked_vendor: { label: "Blocked vendor", tone: "red" },
  identity_mismatch: { label: "Vendor details differ", tone: "red" },
};

export function badgeInfo(b: string): { label: string; tone: Tone } {
  const known = (BADGE_INFO as Record<string, { label: string; tone: Tone }>)[b] ?? EXTRA_BADGES[b];
  return known ?? { label: b.replace(/_/g, " "), tone: "neutral" };
}

/** Plain-English labels for header fields. */
export const FIELD_LABELS: Record<string, string> = {
  vendor_name: "Vendor",
  vendor_address: "Vendor address",
  vendor_tax_id_last4: "Tax ID (last 4)",
  invoice_number: "Invoice #",
  invoice_date: "Invoice date",
  due_date: "Due date",
  work_order_number: "Work order",
  property_or_unit: "Property / unit",
  subtotal: "Subtotal",
  tax: "Tax",
  total: "Total",
  remit_to_bank_details: "Remit-to bank",
  currency: "Currency",
  vendor_id: "Matched vendor",
};

export function fieldLabel(name: string): string {
  return FIELD_LABELS[name] ?? name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export const MONEY_FIELD_NAMES = new Set(["subtotal", "tax", "total"]);
export const DATE_FIELD_NAMES = new Set(["invoice_date", "due_date"]);

/** Plain-English rule names for the viewer. */
export const RULE_TITLES: Record<string, string> = {
  R1: "Approved vendor",
  R2: "Work order matches unit",
  R3: "Matches approved estimate",
  R4: "Not a duplicate",
  R5: "Math adds up",
  R6: "Bank details unchanged",
  R7: "Within spending limit",
  R8: "Dates make sense",
  R9: "Every field readable",
  R10: "No hidden instructions",
};
