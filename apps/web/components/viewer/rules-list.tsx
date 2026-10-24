import { CircleAlert, CircleCheck, OctagonX } from "lucide-react";
import type { RuleResult } from "@/lib/api-types";
import { RULE_TITLES } from "@/lib/badges";
import { maskText } from "@/lib/mask";

export function RulesList({ rules }: { rules: RuleResult[] }) {
  if (rules.length === 0) return <p className="text-slate-600">Checks haven&apos;t run yet.</p>;
  const sorted = [...rules].sort((a, b) => Number(a.passed) - Number(b.passed));
  return (
    <ul className="space-y-2" aria-label="Checks">
      {sorted.map((r) => (
        <li key={r.rule_id} className="flex items-start gap-2">
          {r.passed ? (
            <CircleCheck className="mt-0.5 h-5 w-5 shrink-0 text-green-700" aria-label="Passed" />
          ) : r.severity === "flag" ? (
            <OctagonX className="mt-0.5 h-5 w-5 shrink-0 text-red-700" aria-label="Blocked" />
          ) : (
            <CircleAlert className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" aria-label="Needs a person" />
          )}
          <span>
            <span className="font-semibold text-slate-900">{RULE_TITLES[r.rule_id] ?? r.name}</span>
            <span className="block text-slate-700">{maskText(r.message)}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}
