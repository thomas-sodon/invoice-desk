/** Confidence bands (M5): green >= tenant threshold; red < 0.5 or failed rule; amber otherwise. */
import type { Band } from "./api-types";

export const RED_BELOW = 0.5;

export function bandFor(calibratedConf: number | null, threshold: number, failedRule = false): Band {
  if (failedRule) return "red";
  if (calibratedConf === null || Number.isNaN(calibratedConf)) return "red";
  if (calibratedConf < RED_BELOW) return "red";
  if (calibratedConf >= threshold) return "green";
  return "amber";
}

/** Stroke/fill colors for canvas overlays (WCAG-legible on white paper). */
export const BAND_COLORS: Record<Band, { stroke: string; fill: string; label: string }> = {
  green: { stroke: "#15803d", fill: "rgba(22, 163, 74, 0.14)", label: "Confident" },
  amber: { stroke: "#b45309", fill: "rgba(245, 158, 11, 0.22)", label: "Please check" },
  red: { stroke: "#b91c1c", fill: "rgba(220, 38, 38, 0.18)", label: "Problem" },
};

/** Tailwind classes for chips/legends. */
export const BAND_CLASSES: Record<Band, string> = {
  green: "bg-green-50 text-green-800 ring-green-600/30",
  amber: "bg-amber-50 text-amber-900 ring-amber-600/40",
  red: "bg-red-50 text-red-800 ring-red-600/30",
};

/** Hidden/faint text revealed by screening is always boxed in this red. */
export const HIDDEN_TEXT_COLOR = "#dc2626";
