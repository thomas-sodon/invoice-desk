"use client";

import { Upload } from "lucide-react";
import * as React from "react";
import { ACCEPT_ATTR } from "@/lib/upload";
import { cn } from "@/lib/utils";

/** Drag-and-drop (or click) file picker for JPEG, PNG, HEIC and PDF invoices. */
export function UploadZone({
  onFiles,
  busy,
  compact,
  multiple = true,
  label,
}: {
  onFiles: (files: File[]) => void;
  busy?: boolean;
  compact?: boolean;
  multiple?: boolean;
  label?: string;
}) {
  const [over, setOver] = React.useState(false);
  const input = React.useRef<HTMLInputElement>(null);
  return (
    <div
      onDragOver={(e) => {
        if (!e.dataTransfer.types.includes("Files")) return;
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        if (!e.dataTransfer.files.length) return;
        e.preventDefault();
        setOver(false);
        onFiles(Array.from(e.dataTransfer.files));
      }}
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed bg-white text-center transition-colors",
        compact ? "px-4 py-3 sm:flex-row sm:gap-4" : "px-4 py-10",
        over ? "border-blue-600 bg-blue-50" : "border-slate-300",
      )}
      data-testid="upload-zone"
    >
      <Upload className={cn("text-blue-700", compact ? "h-6 w-6" : "h-10 w-10")} aria-hidden />
      <p className={cn("text-slate-800", compact ? "text-base" : "text-lg")}>
        {label ?? "Drop invoices here: phone photos, scans or PDFs"}
        <span className="block text-sm text-slate-500">JPEG, PNG, HEIC or PDF · up to 10 MB and 5 pages each</span>
      </p>
      <button
        type="button"
        disabled={busy}
        onClick={() => input.current?.click()}
        className="rounded-lg border border-slate-300 px-4 py-2 font-semibold text-slate-800 hover:bg-slate-50 disabled:opacity-50"
      >
        {busy ? "Uploading…" : "Choose files"}
      </button>
      <input
        ref={input}
        type="file"
        className="sr-only"
        accept={ACCEPT_ATTR}
        multiple={multiple}
        aria-label="Choose invoice files"
        onChange={(e) => {
          if (e.target.files?.length) onFiles(Array.from(e.target.files));
          e.target.value = "";
        }}
      />
    </div>
  );
}
