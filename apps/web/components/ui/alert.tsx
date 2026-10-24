import * as React from "react";
import type { Tone } from "@/lib/columns";
import { cn } from "@/lib/utils";

const TONES: Record<Tone, string> = {
  neutral: "border-slate-300 bg-slate-50 text-slate-900",
  green: "border-green-300 bg-green-50 text-green-900",
  amber: "border-amber-300 bg-amber-50 text-amber-950",
  red: "border-red-300 bg-red-50 text-red-900",
  blue: "border-blue-300 bg-blue-50 text-blue-900",
};

export function Alert({ tone = "neutral", className, ...props }: React.HTMLAttributes<HTMLDivElement> & { tone?: Tone }) {
  return <div role="status" className={cn("rounded-lg border px-4 py-3 text-base", TONES[tone], className)} {...props} />;
}
