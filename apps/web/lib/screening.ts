import type { ScreenResult, ScreenSpan } from "./api-types";
import { maskText } from "./mask";

export interface HiddenTextSummary {
  title: string;
  how: string;
  spans: ScreenSpan[];
  bankChange: boolean;
}

const HOW: Record<string, string> = {
  hidden_text: "Text hidden in the PDF (invisible on the page) was revealed.",
  faint_text: "Very faint text on the photo was revealed by enhancing the image.",
  injection_classifier: "The text reads like an instruction aimed at an AI.",
};

/** Plain-English summary of a screening hit, or null when every detector came back clean. */
export function hiddenTextSummary(results: ScreenResult[]): HiddenTextSummary | null {
  const hits = results.filter((r) => r.verdict !== "clean");
  if (hits.length === 0) return null;
  const categories = new Set(hits.flatMap((r) => r.categories));
  const bankChange = categories.has("bank_or_remit_change");
  const title = bankChange
    ? "Hidden instruction detected: attempted bank-account change, blocked"
    : "Hidden instruction detected, blocked";
  const how = hits.map((r) => HOW[r.detector]).filter(Boolean)[0] ?? "Suspicious content was found.";
  const seen = new Set<string>();
  const spans: ScreenSpan[] = [];
  for (const r of hits)
    for (const s of r.spans) {
      const key = `${s.page}:${s.box?.join(",") ?? s.text}`;
      if (seen.has(key)) continue;
      seen.add(key);
      spans.push({ ...s, text: maskText(s.text) });
    }
  return { title, how, spans, bankChange };
}
