/**
 * Defense-in-depth masking (CLAUDE.md invariant 9). The API already masks bank and account
 * numbers to the last 4; the UI masks again before rendering any free text from a document.
 */
const BULLETS = "••••";

/** "000555067890" -> "••••7890". Strings shorter than 5 digits are returned unchanged. */
export function maskNumber(value: string): string {
  const digits = value.replace(/\D/g, "");
  if (digits.length <= 4) return value;
  return `${BULLETS}${digits.slice(-4)}`;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Masks every run of 6+ digits (optionally broken by spaces or dashes) inside free text. ISO dates
 * (YYYY-MM-DD) are left alone so timestamps stay readable.
 */
export function maskText(text: string): string {
  return text.replace(/\d(?:[ -]?\d){5,}/g, (run) => (ISO_DATE.test(run) ? run : maskNumber(run)));
}

/** Structured fields whose values are identifiers, dates or amounts, never bank numbers. */
const STRUCTURED_FIELDS = new Set([
  "invoice_number",
  "invoice_date",
  "due_date",
  "work_order_number",
  "subtotal",
  "tax",
  "total",
  "vendor_tax_id_last4",
]);

/** Field names that hold bank or account details (mirrors invoicedesk.masking.SENSITIVE_KEY_RE). */
export const SENSITIVE_FIELD_RE = /(bank|account|acct|routing|aba|iban|swift|bic|sort_code|card)/i;

/**
 * Masks an extracted field value for display. Invoice and work-order numbers, dates and amounts are
 * shown as read (masking "26-7200" would hide the invoice number); bank fields mask every run of
 * 5+ digits; any other free text masks 6+ digit runs like `maskText`.
 */
export function maskFieldValue(name: string, value: string): string {
  if (SENSITIVE_FIELD_RE.test(name)) return value.replace(/\d(?:[ -]?\d){4,}/g, (run) => maskNumber(run));
  if (STRUCTURED_FIELDS.has(name)) return value;
  return maskText(value);
}
