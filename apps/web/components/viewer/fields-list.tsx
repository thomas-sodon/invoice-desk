"use client";

import type { Field } from "@/lib/api-types";
import { fieldLabel } from "@/lib/badges";
import { BAND_COLORS } from "@/lib/bands";
import { maskText } from "@/lib/mask";
import { cn } from "@/lib/utils";

const DOT: Record<Field["band"], string> = { green: "bg-green-600", amber: "bg-amber-500", red: "bg-red-600" };

export function FieldsList({
  fields,
  selected,
  onSelect,
}: {
  fields: Field[];
  selected: string | null;
  onSelect: (name: string | null) => void;
}) {
  if (fields.length === 0) return <p className="text-slate-600">Nothing was read from this document.</p>;
  return (
    <ul className="divide-y divide-slate-100" aria-label="Fields read from the invoice">
      {fields.map((f) => {
        const display = f.display_value ?? f.value;
        return (
          <li key={f.name}>
            <button
              type="button"
              onClick={() => onSelect(selected === f.name ? null : f.name)}
              className={cn(
                "flex w-full items-start gap-3 px-1 py-2 text-left hover:bg-slate-50",
                selected === f.name && "bg-blue-50",
              )}
              aria-pressed={selected === f.name}
            >
              <span className={cn("mt-2 h-2.5 w-2.5 shrink-0 rounded-full", DOT[f.band])} aria-hidden />
              <span className="min-w-0 flex-1">
                <span className="block text-sm text-slate-500">{fieldLabel(f.name)}</span>
                <span className="block break-words font-medium text-slate-900">{display ? maskText(display) : <em className="text-slate-500">not readable</em>}</span>
                {f.reason && <span className="block text-sm font-medium" style={{ color: BAND_COLORS[f.band].stroke }}>{f.reason}</span>}
              </span>
              {f.calibrated_conf !== null && (
                <span className="shrink-0 text-sm tabular-nums text-slate-500" title="How sure the reader is">
                  {Math.round(f.calibrated_conf * 100)}%
                </span>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
