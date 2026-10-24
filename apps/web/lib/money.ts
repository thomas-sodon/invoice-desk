/**
 * Money helpers. Invoice amounts are integer cents end to end (CLAUDE.md invariant 6); this
 * module never converts cents through floating-point division. API cost is a USD decimal string.
 */
import type { Cents, UsdString } from "./api-types";

const MINUS = "-";

function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** 124000 -> "$1,240.00"; -1250 -> "-$12.50"; null -> "—". Throws on non-integers. */
export function formatCents(cents: Cents | null | undefined, opts: { dropZeroCents?: boolean } = {}): string {
  if (cents === null || cents === undefined) return "—";
  if (!Number.isSafeInteger(cents)) throw new TypeError(`cents must be an integer, got ${cents}`);
  const negative = cents < 0;
  const abs = BigInt(Math.abs(cents));
  const dollars = abs / 100n;
  const rem = abs % 100n;
  const head = `$${groupThousands(dollars.toString())}`;
  const body = opts.dropZeroCents && rem === 0n ? head : `${head}.${rem.toString().padStart(2, "0")}`;
  return negative ? `${MINUS}${body}` : body;
}

/**
 * Parses what a clerk types ("$1,240", "1240.5", "1 240.00") into integer cents using string
 * arithmetic only. Returns null for anything that is not a plain amount or has >2 decimals.
 */
export function parseDollarsToCents(input: string): Cents | null {
  const cleaned = input.trim().replace(/[$,\s]/g, "");
  const m = /^(-)?(\d+)(?:\.(\d{1,2}))?$/.exec(cleaned);
  if (!m) return null;
  const [, sign, whole = "0", frac = ""] = m;
  const cents = BigInt(whole) * 100n + BigInt(frac.padEnd(2, "0") || "0");
  if (cents > BigInt(Number.MAX_SAFE_INTEGER)) return null;
  const n = Number(cents);
  return sign ? -n : n;
}

/** Normalizes a USD decimal string for display: at least `minDecimals` digits, trailing zeros trimmed. */
export function formatUsd(usd: UsdString | null | undefined, minDecimals = 2): string {
  if (usd === null || usd === undefined || usd === "") return "—";
  const m = /^(-)?(\d+)(?:\.(\d+))?$/.exec(usd.trim());
  if (!m) return "—";
  const [, sign, whole = "0", fracRaw = ""] = m;
  let frac = fracRaw.replace(/0+$/, "");
  if (frac.length < minDecimals) frac = frac.padEnd(minDecimals, "0");
  const body = `$${groupThousands(whole.replace(/^0+(?=\d)/, ""))}${frac ? `.${frac}` : ""}`;
  return sign ? `${MINUS}${body}` : body;
}

const USD_SCALE = 8; // numeric(12,8) in the DB

function usdToScaled(usd: UsdString): bigint {
  const m = /^(-)?(\d+)(?:\.(\d+))?$/.exec(usd.trim());
  if (!m) throw new TypeError(`not a USD decimal string: ${usd}`);
  const [, sign, whole = "0", frac = ""] = m;
  const v = BigInt(whole) * 10n ** BigInt(USD_SCALE) + BigInt(frac.slice(0, USD_SCALE).padEnd(USD_SCALE, "0"));
  return sign ? -v : v;
}

function scaledToUsd(v: bigint): UsdString {
  const negative = v < 0n;
  const abs = negative ? -v : v;
  const base = 10n ** BigInt(USD_SCALE);
  const whole = (abs / base).toString();
  const frac = (abs % base).toString().padStart(USD_SCALE, "0").replace(/0+$/, "");
  return `${negative ? "-" : ""}${whole}${frac ? `.${frac}` : ""}`;
}

/** Exact decimal addition of USD strings (for the live batch cost meter). */
export function addUsd(...values: (UsdString | null | undefined)[]): UsdString {
  let total = 0n;
  for (const v of values) if (v) total += usdToScaled(v);
  return scaledToUsd(total);
}

/** Compare two USD strings: -1, 0, 1. */
export function compareUsd(a: UsdString, b: UsdString): number {
  const d = usdToScaled(a) - usdToScaled(b);
  return d === 0n ? 0 : d < 0n ? -1 : 1;
}

/** Cost-meter display: cents precision from $0.10 up ("$0.27"), four decimals below ("$0.0101"). */
export function formatUsdMeter(usd: UsdString | null | undefined): string {
  if (!usd) return "$0.00";
  const scaled = usdToScaled(usd);
  const tenCents = 10n ** BigInt(USD_SCALE - 1);
  const abs = scaled < 0n ? -scaled : scaled;
  if (abs >= tenCents) {
    const unit = 10n ** BigInt(USD_SCALE - 2);
    const cents = (abs + unit / 2n) / unit; // round half up to whole cents
    const n = Number(cents);
    return formatCents(scaled < 0n ? -n : n);
  }
  const unit = 10n ** BigInt(USD_SCALE - 4);
  const tenThousandths = (abs + unit / 2n) / unit;
  const s = `${scaled < 0n ? "-" : ""}0.${tenThousandths.toString().padStart(4, "0")}`;
  return formatUsd(s, 2);
}
