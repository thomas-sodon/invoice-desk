"use client";

import * as React from "react";
import type { Field, PageImage, Redaction, ScreenResult } from "@/lib/api-types";
import { OverlayCanvas } from "./page-overlay";

const TARGET_LONG_EDGE = 1600;

/**
 * Renders page 1 of a PDF with pdf.js onto a canvas and draws the same overlays on top. Box
 * coordinates are in processed-image px (1,600 px long edge), so the page is mapped to that space.
 */
export function PdfPage({
  url,
  fields,
  screen,
  selected,
  onSelect,
  redactions,
}: {
  redactions?: Redaction[];
  url: string;
  fields: Field[];
  screen: ScreenResult[];
  selected: string | null;
  onSelect?: (f: string | null) => void;
}) {
  const wrap = React.useRef<HTMLDivElement>(null);
  const canvasRef = React.useRef<HTMLCanvasElement>(null);
  const [page, setPage] = React.useState<PageImage | null>(null);
  const [width, setWidth] = React.useState(0);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  React.useEffect(() => {
    if (width <= 0) return;
    let cancelled = false;
    // A resize starts a new render; the previous one must be cancelled first, because pdf.js
    // refuses two concurrent renders on one canvas.
    let task: { cancel: () => void; promise: Promise<unknown> } | null = null;
    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();
        const pdf = await pdfjs.getDocument({ url, isEvalSupported: false }).promise;
        const p = await pdf.getPage(1);
        const base = p.getViewport({ scale: 1 });
        const toProcessed = TARGET_LONG_EDGE / Math.max(base.width, base.height);
        const scale = width / base.width;
        const viewport = p.getViewport({ scale });
        const canvas = canvasRef.current;
        if (!canvas || cancelled) return;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.round(viewport.width * dpr);
        canvas.height = Math.round(viewport.height * dpr);
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        task = p.render({ canvasContext: ctx, viewport, transform: dpr === 1 ? undefined : [dpr, 0, 0, dpr, 0, 0] });
        await task.promise;
        if (!cancelled)
          setPage({ index: 0, width: Math.round(base.width * toProcessed), height: Math.round(base.height * toProcessed), image_url: url });
        void pdf.destroy();
      } catch (e) {
        if (e instanceof Error && e.name === "RenderingCancelledException") return;
        if (!cancelled) setError("This PDF couldn't be displayed in the browser.");
      }
    })();
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [url, width]);

  return (
    <div ref={wrap} className="relative w-full overflow-hidden rounded-lg border border-slate-200 bg-white">
      {error ? <p className="p-6 text-slate-600">{error}</p> : <canvas ref={canvasRef} className="block" aria-label="Invoice PDF" />}
      {page && (
        <OverlayCanvas
          page={page}
          width={width}
          height={(width * page.height) / page.width}
          fields={fields}
          screen={screen}
          selected={selected}
          showFields
          onSelect={onSelect}
          redactions={redactions}
        />
      )}
    </div>
  );
}
