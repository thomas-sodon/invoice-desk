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
