/** Review-queue helpers: which fields need a person, and turning what they type into API values. */
import type { DocumentDetail, Field } from "./api-types";
import { DATE_FIELD_NAMES, MONEY_FIELD_NAMES } from "./badges";
import { formatCents, parseDollarsToCents } from "./money";

export const REQUIRED_FIELDS = ["vendor_name", "invoice_number", "invoice_date", "work_order_number", "property_or_unit", "total"] as const;

/** Fields a reviewer should look at, most important first. */
export function problemFields(doc: Pick<DocumentDetail, "fields" | "rule_results">): string[] {
  const out: string[] = [];
  const add = (n: string) => !out.includes(n) && out.push(n);
  for (const f of doc.fields) if (f.band !== "green") add(f.name);
  for (const r of doc.rule_results) if (!r.passed) r.fields.forEach(add);
  const present = new Set(doc.fields.filter((f) => f.value !== null).map((f) => f.name));
  for (const n of REQUIRED_FIELDS) if (!present.has(n)) add(n);
  return out.filter((n) => n !== "remit_to_bank_details");
}

/** Suggested total = line items + tax, when every amount is known. */
export function suggestedTotal(doc: Pick<DocumentDetail, "fields" | "line_items">): number | null {
  if (doc.line_items.length === 0) return null;
  let sum = 0;
  for (const li of doc.line_items) {
    if (li.amount_cents === null) return null;
    sum += li.amount_cents;
  }
  const tax = doc.fields.find((f) => f.name === "tax");
  if (tax?.value && /^-?\d+$/.test(tax.value)) sum += Number(tax.value);
  return sum;
}

export type Parsed = { ok: true; value: string; display: string } | { ok: false; error: string };

export function parseCorrection(field: string, input: string): Parsed {
  const v = input.trim();
  if (!v) return { ok: false, error: "Enter a value." };
  if (MONEY_FIELD_NAMES.has(field)) {
    const cents = parseDollarsToCents(v);
    if (cents === null) return { ok: false, error: "Enter an amount like 1,240.00" };
    return { ok: true, value: String(cents), display: formatCents(cents) };
  }
  if (DATE_FIELD_NAMES.has(field)) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
    const d = m ? new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]))) : null;
    if (!m || !d || d.getUTCDate() !== Number(m[3])) return { ok: false, error: "Enter a date as YYYY-MM-DD" };
    return { ok: true, value: v, display: v };
  }
  return { ok: true, value: v, display: v };
}

/** What to pre-fill in the edit box for a field. */
export function editableValue(f: Field | undefined, field: string): string {
  if (!f || f.value === null) return "";
  if (MONEY_FIELD_NAMES.has(field) && /^-?\d+$/.test(f.value)) return formatCents(Number(f.value)).replace("$", "");
  return f.value;
}
