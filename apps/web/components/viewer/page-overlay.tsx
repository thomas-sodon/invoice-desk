"use client";

import * as React from "react";
import type { Field, PageImage, Redaction, ScreenResult } from "@/lib/api-types";
import { hitTest, overlayRects, type OverlayRect } from "@/lib/overlay";

function draw(canvas: HTMLCanvasElement, rects: OverlayRect[], width: number, height: number) {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  for (const r of rects) {
    ctx.fillStyle = r.fill;
    ctx.fillRect(r.x, r.y, r.w, r.h);
    ctx.strokeStyle = r.stroke;
    ctx.lineWidth = r.lineWidth;
    ctx.strokeRect(r.x, r.y, r.w, r.h);
    if (r.inner && r.h >= 9) {
      const size = Math.max(7, Math.min(18, r.h * (r.kind === "redaction" ? 0.7 : 0.42)));
      ctx.font = `600 ${size}px system-ui, sans-serif`;
      ctx.fillStyle = r.kind === "redaction" ? "#ffffff" : "#991b1b";
      const words = r.inner.split(/\s+/);
      const lines: string[] = [];
      let cur = "";
      for (const w of words) {
        const test = cur ? `${cur} ${w}` : w;
        if (ctx.measureText(test).width > r.w - 10 && cur) {
          lines.push(cur);
          cur = w;
        } else cur = test;
      }
      if (cur) lines.push(cur);
      const maxLines = Math.max(1, Math.floor((r.h - 4) / (size * 1.15)));
      const shown = lines.slice(0, maxLines);
      const top = r.y + (r.h - shown.length * size * 1.15) / 2 + size * 0.9;
      shown.forEach((line, i) => ctx.fillText(line, r.x + 5, top + i * size * 1.15, r.w - 10));
    }
    if (r.label && (r.kind === "hidden" || (r.kind === "field" && r.band !== "green"))) {
      const text = r.label;
      ctx.font = "600 13px system-ui, sans-serif";
      const tw = ctx.measureText(text).width + 10;
      const ty = r.y - 20 < 0 ? r.y + r.h + 2 : r.y - 20;
      ctx.fillStyle = r.stroke;
      ctx.fillRect(r.x, ty, Math.min(tw, width - r.x), 18);
      ctx.fillStyle = "#ffffff";
      ctx.fillText(text, r.x + 5, ty + 13, width - r.x - 10);
    }
  }
}

/** Shared canvas layer that sits on top of any page rendering (img or pdf.js canvas). */
export function OverlayCanvas({
  page,
  width,
  height,
  fields,
  screen,
  selected,
  showFields,
  onSelect,
  redactions,
  onDrawn,
}: {
  redactions?: Redaction[];
  /** Called after the overlay (including any redaction boxes) has been painted. */
  onDrawn?: () => void;
  page: PageImage;
  width: number;
  height: number;
  fields: Field[];
  screen: ScreenResult[];
  selected: string | null;
  showFields: boolean;
  onSelect?: (field: string | null) => void;
}) {
  const ref = React.useRef<HTMLCanvasElement>(null);
  const onDrawnRef = React.useRef(onDrawn);
  onDrawnRef.current = onDrawn;
  const rects = React.useMemo(
    () => overlayRects({ page, displayWidth: width, fields, screen, selected, showFields, redactions }),
    [page, width, fields, screen, selected, showFields, redactions],
  );
  React.useEffect(() => {
    if (ref.current && width > 0) {
      draw(ref.current, rects, width, height);
      onDrawnRef.current?.();
    }
  }, [rects, width, height]);
  return (
    <canvas
      ref={ref}
      data-testid="overlay-canvas"
      data-rects={rects.length}
      className="absolute left-0 top-0 cursor-pointer"
      onClick={(e) => {
        const box = e.currentTarget.getBoundingClientRect();
        const hit = hitTest(rects.filter((r) => r.kind === "field"), e.clientX - box.left, e.clientY - box.top);
        onSelect?.(hit ? hit.key.replace(/^field:/, "") : null);
      }}
    />
  );
}

/** A page image with box overlays colored by band, plus revealed hidden text boxed in red. */
export function PageOverlay(props: {
  page: PageImage;
  fields: Field[];
  screen: ScreenResult[];
  selected: string | null;
  showFields?: boolean;
  onSelect?: (field: string | null) => void;
  alt: string;
  redactions?: Redaction[];
}) {
  const { page, alt } = props;
  const wrap = React.useRef<HTMLDivElement>(null);
  const [size, setSize] = React.useState({ w: 0, h: 0 });
  // When the page has printed bank details, keep the image hidden until the opaque redaction
  // boxes are painted over it, so a full account number never flashes on screen (invariant 9).
  const needsCover = (props.redactions ?? []).some((r) => (r.page ?? 0) === page.index);
  const [covered, setCovered] = React.useState(false);

  React.useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const update = () => {
      const w = el.clientWidth;
      setSize({ w, h: (w * page.height) / Math.max(1, page.width) });
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [page.width, page.height]);

  return (
    <div ref={wrap} className="relative w-full overflow-hidden rounded-lg border border-slate-200 bg-white" style={{ aspectRatio: `${page.width} / ${page.height}` }}>
      {/* eslint-disable-next-line @next/next/no-img-element -- signed URLs from the API, sized by the overlay */}
      <img src={page.image_url} alt={alt} className="absolute left-0 top-0 h-full w-full select-none"
        style={{ visibility: needsCover && !covered ? "hidden" : "visible" }}
        data-covered={needsCover ? String(covered) : undefined}
        draggable={false}
      />
      <OverlayCanvas
        page={page}
        width={size.w}
        height={size.h}
        fields={props.fields}
        screen={props.screen}
        selected={props.selected}
        showFields={props.showFields ?? true}
        onSelect={props.onSelect}
        redactions={props.redactions}
        onDrawn={() => setCovered(true)}
      />
    </div>
  );
}
