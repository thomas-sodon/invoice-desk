import * as React from "react";
import type { Tone } from "@/lib/columns";
import { cn } from "@/lib/utils";

const TONES: Record<Tone, string> = {
  neutral: "bg-slate-100 text-slate-800 ring-slate-300",
  green: "bg-green-50 text-green-800 ring-green-600/30",
  amber: "bg-amber-50 text-amber-900 ring-amber-600/40",
  red: "bg-red-50 text-red-800 ring-red-600/30",
  blue: "bg-blue-50 text-blue-800 ring-blue-600/30",
};

export function Chip({ tone = "neutral", className, ...props }: React.HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={cn("inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-sm font-medium ring-1 ring-inset", TONES[tone], className)}
      {...props}
    />
  );
}
