/**
 * Overlay geometry: field boxes are in processed-image pixels (1,600 px long edge), so drawing them
 * on a displayed page is a single uniform scale. Pure functions; the canvas component draws them.
 */
import type { Band, Box, Field, PageImage, Redaction, ScreenResult } from "./api-types";
import { BAND_COLORS, HIDDEN_TEXT_COLOR } from "./bands";
import { maskText } from "./mask";

export interface OverlayRect {
  key: string;
  kind: "field" | "hidden" | "redaction";
  x: number;
  y: number;
  w: number;
  h: number;
  stroke: string;
  fill: string;
  lineWidth: number;
  label: string | null;
  band: Band | null;
  /** Text painted inside the box (masked revealed hidden text, or a redaction label). */
  inner: string | null;
}

export function scaleBox(box: Box, scale: number, pad = 0): { x: number; y: number; w: number; h: number } {
  const [x0, y0, x1, y1] = box;
  const left = Math.min(x0, x1);
  const top = Math.min(y0, y1);
  return {
    x: (left - pad) * scale,
    y: (top - pad) * scale,
    w: (Math.abs(x1 - x0) + 2 * pad) * scale,
    h: (Math.abs(y1 - y0) + 2 * pad) * scale,
  };
}

export function overlayRects(opts: {
  page: PageImage;
  displayWidth: number;
  fields: Field[];
  screen: ScreenResult[];
  selected?: string | null;
  showFields?: boolean;
  redactions?: Redaction[];
}): OverlayRect[] {
  const { page, displayWidth, fields, screen, selected = null, showFields = true, redactions = [] } = opts;
  if (page.width <= 0 || displayWidth <= 0) return [];
  const scale = displayWidth / page.width;
  const rects: OverlayRect[] = [];
  if (showFields) {
    for (const f of fields) {
      if (!f.box || (f.page ?? 0) !== page.index) continue;
      const c = BAND_COLORS[f.band];
      const isSel = selected === f.name;
      rects.push({
        key: `field:${f.name}`,
        kind: "field",
        ...scaleBox(f.box, scale, 3),
        stroke: c.stroke,
        fill: c.fill,
        lineWidth: isSel ? 4 : f.band === "green" ? 2 : 3,
        label: f.band === "green" && !isSel ? null : (f.reason ?? null),
        band: f.band,
        inner: null,
      });
    }
  }
  const seen = new Set<string>();
  for (const r of screen) {
    for (const [i, s] of r.spans.entries()) {
      if (!s.box || (s.page ?? 0) !== page.index) continue;
      const k = s.box.join(",");
      if (seen.has(k)) continue;
      seen.add(k);
      rects.push({
        key: `hidden:${r.detector}:${i}`,
        kind: "hidden",
        ...scaleBox(s.box, scale, 6),
        stroke: HIDDEN_TEXT_COLOR,
        // Opaque so the original pixels (which may hold a full account number) never show; the
        // revealed text is repainted inside, legibly and masked.
        fill: "rgba(254, 242, 242, 0.97)",
        lineWidth: 4,
        label: seen.size === 1 ? "Revealed hidden text" : null,
        band: null,
        inner: maskText(s.text),
      });
    }
  }
  // Redactions are drawn last so nothing paints over them.
  for (const [i, r] of redactions.entries()) {
    if ((r.page ?? 0) !== page.index) continue;
    rects.push({
      key: `redaction:${i}`,
      kind: "redaction",
      ...scaleBox(r.box, scale, 2),
      stroke: "#334155",
      fill: "#334155",
      lineWidth: 1,
      label: null,
      band: null,
      inner: r.label ? maskText(r.label) : "Bank details hidden",
    });
  }
  return rects;
}

/** Index of the field whose box contains the point (display px), smallest box first. */
export function hitTest(rects: OverlayRect[], x: number, y: number): OverlayRect | null {
  const hits = rects.filter((r) => x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h);
  hits.sort((a, b) => a.w * a.h - b.w * b.h);
  return hits[0] ?? null;
}
