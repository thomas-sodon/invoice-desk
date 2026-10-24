"use client";

import { useDraggable } from "@dnd-kit/core";
import { CSS } from "@dnd-kit/utilities";
import type { DocumentCard as Card } from "@/lib/api-types";
import { badgeInfo } from "@/lib/badges";
import { STATUS_LABELS } from "@/lib/columns";
import { formatCents } from "@/lib/money";
import { cn } from "@/lib/utils";
import { Chip } from "../ui/chip";

const EDGE: Record<string, string> = {
  ready_to_pay: "border-l-green-600",
  posted: "border-l-green-800",
  needs_review: "border-l-amber-500",
  flagged: "border-l-red-600",
  rejected: "border-l-slate-400",
};

export function DocCard({
  card,
  onOpen,
  compact,
  dense = false,
}: {
  card: Card;
  onOpen: (id: string) => void;
  compact: boolean;
  /** Hero layout: smaller headline, no badges, no drag handle. */
  dense?: boolean;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({ id: card.id, data: { status: card.status } });
  const inFlight = card.column === "received" || card.column === "extracted" || card.column === "validated";
  const title = card.vendor_name ?? card.filename;
  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform) }}
      className={cn(
        "list-none rounded-lg border border-l-4 border-slate-200 bg-white shadow-sm transition-shadow",
        EDGE[card.column] ?? "border-l-slate-300",
        isDragging && "z-20 shadow-xl ring-2 ring-blue-500",
      )}
      data-testid="doc-card"
      data-status={card.status}
    >
      <div className="flex items-stretch">
        <button
          type="button"
          onClick={() => onOpen(card.id)}
          className="min-w-0 flex-1 px-3 py-2 text-left focus-visible:outline-2 focus-visible:outline-blue-600"
          aria-label={`Open ${title}`}
        >
          <span className="flex items-baseline justify-between gap-2">
            <span className="truncate font-semibold text-slate-900">{title}</span>
            <span className="shrink-0 font-semibold tabular-nums text-slate-900">{formatCents(card.total_cents)}</span>
          </span>
          {!compact && (
            <>
              {card.headline ? (
                <span
                  className={cn(
                    "mt-1 block leading-snug",
                    dense ? "text-sm" : "text-[0.95rem]",
                    card.column === "flagged" ? "font-semibold text-red-800" : "text-amber-900",
                  )}
                >
                  {card.headline}
                </span>
              ) : (
                inFlight && <span className="mt-1 block text-sm text-slate-500">{STATUS_LABELS[card.status]}…</span>
              )}
              {!dense && card.badges.length > 0 && (
                <span className="mt-1.5 flex flex-wrap gap-1">
                  {card.badges.slice(0, 3).map((b) => {
                    const info = badgeInfo(b);
                    return (
                      <Chip key={b} tone={info.tone} className="px-2 text-xs">
                        {info.label}
                      </Chip>
                    );
                  })}
                </span>
              )}
            </>
          )}
          {compact && card.invoice_number && <span className="block truncate text-sm text-slate-500">{card.invoice_number}</span>}
        </button>
        {!dense && (
        <span
          {...listeners}
          {...attributes}
          aria-label={`Drag ${title}`}
          className="flex w-6 shrink-0 cursor-grab touch-none items-center justify-center rounded-r-lg text-slate-400 hover:bg-slate-50"
        >
          ⋮⋮
        </span>
        )}
      </div>
    </li>
  );
}
