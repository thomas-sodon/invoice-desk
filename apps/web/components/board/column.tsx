"use client";

import { useDroppable } from "@dnd-kit/core";
import type { DocumentCard, KanbanColumn } from "@/lib/api-types";
import { COLUMN_LABELS, COLUMN_TONE } from "@/lib/columns";
import { cn } from "@/lib/utils";
import { DocCard } from "./doc-card";

const HEAD: Record<string, string> = {
  green: "text-green-800 bg-green-50 border-green-200",
  amber: "text-amber-900 bg-amber-50 border-amber-200",
  red: "text-red-800 bg-red-50 border-red-200",
  blue: "text-blue-800 bg-blue-50 border-blue-200",
  neutral: "text-slate-700 bg-slate-100 border-slate-200",
};

export function Column({
  column,
  cards,
  onOpen,
  wide,
  dense,
}: {
  dense?: boolean;
  column: KanbanColumn;
  cards: DocumentCard[];
  onOpen: (id: string) => void;
  wide?: boolean;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: column });
  const tone = COLUMN_TONE[column];
  const compact = column === "ready_to_pay" || column === "posted";
  return (
    <section
      ref={setNodeRef}
      aria-label={`${COLUMN_LABELS[column]} (${cards.length})`}
      data-column={column}
      className={cn(
        "flex min-w-0 flex-col rounded-xl border border-slate-200 bg-slate-100/70",
        isOver && "ring-2 ring-blue-500",
        wide && "xl:col-span-2",
      )}
    >
      <h2 className={cn("flex items-center justify-between rounded-t-xl border-b px-3 py-2 text-base font-bold", HEAD[tone])}>
        <span>{COLUMN_LABELS[column]}</span>
        <span className="rounded-full bg-white/80 px-2 text-lg tabular-nums" data-testid={`count-${column}`}>
          {cards.length}
        </span>
      </h2>
      <ul className="flex flex-col gap-2 p-2">
        {cards.map((c) => (
          <DocCard key={c.id} card={c} onOpen={onOpen} compact={compact} dense={dense} />
        ))}
        {cards.length === 0 && <li className="list-none px-1 py-3 text-center text-sm text-slate-400">Empty</li>}
      </ul>
    </section>
  );
}

export function RejectTray({ count }: { count: number }) {
  const { setNodeRef, isOver } = useDroppable({ id: "reject-tray" });
  return (
    <div
      ref={setNodeRef}
      className={cn(
        "rounded-xl border-2 border-dashed border-slate-300 px-4 py-3 text-center text-slate-600",
        isOver && "border-red-500 bg-red-50 text-red-800",
      )}
    >
      Drop a held invoice here to reject it · {count} rejected
    </div>
  );
}
