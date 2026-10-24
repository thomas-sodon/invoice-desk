"use client";

import { Check } from "lucide-react";
import * as React from "react";
import { useApi } from "@/components/api-provider";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import type { Correction, DocumentDetail } from "@/lib/api-types";
import { DATE_FIELD_NAMES, fieldLabel } from "@/lib/badges";
import { editableValue, parseCorrection, problemFields, suggestedTotal } from "@/lib/corrections";
import { formatCents } from "@/lib/money";

/** One-click field correction (M10): confirm or fix the flagged fields, then re-run the checks. */
export function CorrectionForm({ doc, onDone }: { doc: DocumentDetail; onDone: () => void }) {
  const { api } = useApi();
  const names = React.useMemo(() => problemFields(doc), [doc]);
  const [values, setValues] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const suggestion = suggestedTotal(doc);

  React.useEffect(() => {
    const init: Record<string, string> = {};
    for (const n of names) init[n] = editableValue(doc.fields.find((f) => f.name === n), n);
    setValues(init);
  }, [doc, names]);

  if (doc.status !== "NeedsReview") return null;

  const submit = async (corrections: Correction[]) => {
    if (!api) return;
    setBusy(true);
    setError(null);
    try {
      await api.correct(doc.id, corrections);
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const saveOne = (name: string, raw: string) => {
    const p = parseCorrection(name, raw);
    if (!p.ok) return setError(`${fieldLabel(name)}: ${p.error}`);
    void submit([{ field: name, value: p.value }]);
  };

  const saveAll = () => {
    const out: Correction[] = [];
    for (const n of names) {
      const p = parseCorrection(n, values[n] ?? "");
      if (!p.ok) return setError(`${fieldLabel(n)}: ${p.error}`);
      out.push({ field: n, value: p.value });
    }
    void submit(out);
  };

  if (names.length === 0)
    return <p className="text-slate-700">No field needs fixing; this one is waiting on an approval or a vendor reply.</p>;

  return (
    <div className="space-y-3">
      {names.map((n) => {
        const isTotalSuggestion = n === "total" && suggestion !== null;
        return (
          <div key={n} className="rounded-lg border border-amber-300 bg-amber-50/60 p-3">
            <label className="block text-sm font-semibold text-slate-800" htmlFor={`fix-${n}`}>
              {fieldLabel(n)}
            </label>
            <div className="mt-1 flex flex-wrap gap-2">
              <input
                id={`fix-${n}`}
                type={DATE_FIELD_NAMES.has(n) ? "date" : "text"}
                inputMode={n === "total" || n === "subtotal" || n === "tax" ? "decimal" : undefined}
                className="h-11 min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-3 text-base"
                value={values[n] ?? ""}
                onChange={(e) => setValues((v) => ({ ...v, [n]: e.target.value }))}
                onKeyDown={(e) => e.key === "Enter" && saveOne(n, values[n] ?? "")}
              />
              <Button variant="success" disabled={busy} onClick={() => saveOne(n, values[n] ?? "")}>
                <Check className="h-4 w-4" /> Confirm
              </Button>
            </div>
            {isTotalSuggestion && (
              <Button
                size="sm"
                variant="outline"
                className="mt-2"
                disabled={busy}
                onClick={() => void submit([{ field: "total", value: String(suggestion) }])}
              >
                Use {formatCents(suggestion)} (line items + tax)
              </Button>
            )}
          </div>
        );
      })}
      {names.length > 1 && (
        <Button disabled={busy} onClick={saveAll}>
          Save all and re-check
        </Button>
      )}
      {error && <Alert tone="red">{error}</Alert>}
      <p className="text-sm text-slate-500">Your fix is saved, the checks run again, and it becomes a labeled example for testing.</p>
    </div>
  );
}
