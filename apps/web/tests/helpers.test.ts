import { describe, expect, it } from "vitest";
import type { DocumentDetail, Field, Me, ScreenResult } from "@/lib/api-types";
import { approvalKindFor, approvalProgress, approveGate } from "@/lib/approvals";
import { badgeInfo } from "@/lib/badges";
import { parseCorrection, problemFields, suggestedTotal, editableValue } from "@/lib/corrections";
import { maskNumber, maskText } from "@/lib/mask";
import { formatMetric } from "@/lib/metrics";
import { hitTest, overlayRects, scaleBox } from "@/lib/overlay";
import { hiddenTextSummary } from "@/lib/screening";
import { SseParser, parseBoardEvent } from "@/lib/sse";
import { splitUploads, uploadProblem } from "@/lib/upload";
import fixtures from "@/lib/mock/fixtures/demo-batch.json";

const docs = (fixtures as unknown as { documents: DocumentDetail[] }).documents;
const byFile = (p: string) => docs.find((d) => d.filename.startsWith(p)) as DocumentDetail;

describe("masking", () => {
  it("keeps only the last 4", () => {
    expect(maskNumber("000555067890")).toBe("••••7890");
    expect(maskNumber("1234")).toBe("1234");
    expect(maskText("Routing 110000000, Acct 000999004991")).toBe("Routing ••••0000, Acct ••••4991");
    expect(maskText("acct 0005-5506-7890 now")).toBe("acct ••••7890 now");
    expect(maskText("Invoice INV-2231 for $1,240.00 on 2026-10-24")).toBe("Invoice INV-2231 for $1,240.00 on 2026-10-24");
  });
});

describe("SSE parser", () => {
  it("parses events split across chunks and ignores keepalives", () => {
    const p = new SseParser();
    expect(p.push(": keepalive\n\n")).toEqual([]);
    expect(p.push('data: {"a":')).toEqual([]);
    const out = p.push('1}\n\ndata: x\r\ndata: y\r\n\r\n');
    expect(out.map((m) => m.data)).toEqual(['{"a":1}', "x\ny"]);
  });
  it("tracks event names and ids", () => {
    const p = new SseParser();
    const [m] = p.push("event: board\nid: 7\ndata: z\n\n");
    expect(m).toEqual({ event: "board", id: "7", data: "z" });
  });
  it("turns data into BoardEvents and drops junk", () => {
    expect(parseBoardEvent("not json")).toBeNull();
    expect(parseBoardEvent('{"type":"nope"}')).toBeNull();
    const e = parseBoardEvent('{"type":"batch.cost_updated","tenant_id":"maple_row","batch_cost_usd":"0.27","ts":"t"}');
    expect(e?.batch_cost_usd).toBe("0.27");
    expect(e?.document_id).toBeNull();
  });
});

describe("overlays", () => {
  const page = { index: 0, width: 1237, height: 1600, image_url: "x" };
  it("scales processed-pixel boxes to the displayed width", () => {
    expect(scaleBox([100, 200, 300, 260], 0.5)).toEqual({ x: 50, y: 100, w: 100, h: 30 });
    expect(scaleBox([300, 260, 100, 200], 1, 2)).toEqual({ x: 98, y: 198, w: 204, h: 64 });
  });
  it("colors field boxes by band and highlights the selection", () => {
    const fields: Field[] = [
      { name: "total", value: null, display_value: null, raw_conf: 0.6, calibrated_conf: 0.35, band: "amber", page: 0, box: [10, 10, 50, 30], word_ids: [], reason: "Total unreadable: please confirm" },
      { name: "vendor_name", value: "X", display_value: "X", raw_conf: 1, calibrated_conf: 0.97, band: "green", page: 0, box: [0, 0, 400, 100], word_ids: [], reason: null },
      { name: "due_date", value: "2026-01-01", display_value: "x", raw_conf: 1, calibrated_conf: 0.97, band: "green", page: 1, box: [0, 0, 1, 1], word_ids: [], reason: null },
    ];
    const rects = overlayRects({ page, displayWidth: 618.5, fields, screen: [], selected: "vendor_name" });
    expect(rects).toHaveLength(2);
    expect(rects[0]?.stroke).toBe("#b45309");
    expect(rects[0]?.label).toBe("Total unreadable: please confirm");
    expect(rects[1]?.lineWidth).toBe(4);
    expect(hitTest(rects, 10, 8)?.key).toBe("field:total");
    expect(hitTest(rects, 900, 900)).toBeNull();
    expect(overlayRects({ page, displayWidth: 0, fields, screen: [] })).toEqual([]);
    expect(overlayRects({ page, displayWidth: 600, fields, screen: [], showFields: false })).toEqual([]);
  });
  it("boxes hidden text in red, opaque, with masked text, and paints redactions last", () => {
    const doc = byFile("13-");
    const rects = overlayRects({ page, displayWidth: 1237, fields: [], screen: doc.screen_results, redactions: [{ page: 0, box: [1, 1, 9, 9], label: "Acct 000999237128" }] });
    const hidden = rects.filter((r) => r.kind === "hidden");
    expect(hidden).toHaveLength(2);
    expect(hidden.every((r) => r.stroke === "#dc2626")).toBe(true);
    expect(hidden[0]?.label).toBe("Revealed hidden text");
    expect(hidden[1]?.label).toBeNull();
    expect(hidden.map((r) => r.inner).join(" ")).not.toMatch(/\d{6,}/);
    expect(rects.at(-1)?.kind).toBe("redaction");
    expect(rects.at(-1)?.inner).toBe("Acct ••••7128");
  });
});

describe("hidden-text summary", () => {
  it("uses the demo wording for bank-change attempts", () => {
    const s = hiddenTextSummary(byFile("18-").screen_results);
    expect(s?.title).toBe("Hidden instruction detected: attempted bank-account change, blocked");
    expect(s?.bankChange).toBe(true);
    expect(s?.how).toMatch(/hidden in the PDF/);
    expect(JSON.stringify(s?.spans)).not.toMatch(/\d{6,}/);
  });
  it("is null for clean documents and generic for other categories", () => {
    expect(hiddenTextSummary(byFile("01-").screen_results)).toBeNull();
    const other: ScreenResult[] = [{ detector: "injection_classifier", verdict: "suspicious", categories: ["instruction_to_ai"], spans: [] }];
    expect(hiddenTextSummary(other)?.title).toBe("Hidden instruction detected, blocked");
  });
});

describe("corrections", () => {
  it("finds the unreadable handwritten total and suggests line items + tax", () => {
    const d = byFile("03-");
    expect(problemFields(d)[0]).toBe("total");
    const lines = d.line_items.reduce((s, l) => s + (l.amount_cents ?? 0), 0);
    expect(suggestedTotal(d)).toBe(lines + Number(d.fields.find((f) => f.name === "tax")?.value ?? 0));
  });
  it("parses money and dates into API values", () => {
    expect(parseCorrection("total", "1,240.00")).toEqual({ ok: true, value: "124000", display: "$1,240.00" });
    expect(parseCorrection("total", "12.345").ok).toBe(false);
    expect(parseCorrection("invoice_date", "2026-02-30").ok).toBe(false);
    expect(parseCorrection("invoice_date", "2026-02-28")).toMatchObject({ ok: true, value: "2026-02-28" });
    expect(parseCorrection("vendor_name", "  ")).toEqual({ ok: false, error: "Enter a value." });
    expect(parseCorrection("work_order_number", " WO-1187 ")).toMatchObject({ ok: true, value: "WO-1187" });
  });
  it("pre-fills money as dollars", () => {
    const f = byFile("01-").fields.find((x) => x.name === "total");
    expect(editableValue(f, "total")).toMatch(/^\d[\d,]*\.\d{2}$/);
    expect(editableValue(undefined, "total")).toBe("");
  });
  it("lists missing required fields and never the bank field", () => {
    const d = { fields: [], rule_results: [{ rule_id: "R6", name: "bank_change", passed: false, severity: "flag" as const, message: "", fields: ["remit_to_bank_details"], details: {} }] };
    expect(problemFields(d)).toEqual(["vendor_name", "invoice_number", "invoice_date", "work_order_number", "property_or_unit", "total"]);
  });
});

describe("approval gate", () => {
  const clerk: Me = { id: "dana", email: "d", display_name: "Dana", role: "clerk" };
  const approver: Me = { id: "priya", email: "p", display_name: "Priya", role: "approver" };
  const owner: Me = { id: "sam", email: "s", display_name: "Sam", role: "owner" };
  const flagged = { ...byFile("18-"), uploaded_by: "priya", approvals: [] };
  it("knows the kind of hold", () => {
    expect(approvalKindFor(byFile("18-"))).toBe("bank_change");
    expect(approvalKindFor(byFile("21-"))).toBe("duplicate");
    expect(approvalKindFor(byFile("11-"))).toBe("new_vendor");
    expect(approvalKindFor(byFile("01-"))).toBeNull();
  });
  it("hides approve from clerks and from the uploader", () => {
    expect(approveGate(flagged, clerk)).toMatchObject({ allowed: false, reason: "clerk" });
    expect(approveGate(flagged, approver)).toMatchObject({ allowed: false, reason: "uploader" });
    expect(approveGate(flagged, owner)).toEqual({ allowed: true });
  });
  it("asks for a second, different person", () => {
    const once = { ...flagged, approvals: [{ kind: "bank_change", approver_id: "sam", decision: "approve" as const, callback_attested: true, created_at: "" }] };
    expect(approveGate(once, owner)).toMatchObject({ allowed: false, reason: "already_approved" });
    expect(approvalProgress(once)).toEqual({ have: 1, need: 2, callback: true, rejected: false });
  });
  it("is closed for documents not waiting on approval", () => {
    expect(approveGate(byFile("01-"), owner)).toMatchObject({ allowed: false, reason: "not_waiting" });
    expect(approveGate(byFile("03-"), owner)).toMatchObject({ allowed: false, reason: "not_waiting" });
  });
});

describe("uploads", () => {
  const f = (name: string, type: string, size = 1000) => ({ name, type, size });
  it("accepts JPEG, PNG, HEIC and PDF up to 10 MB", () => {
    expect(uploadProblem(f("a.jpg", "image/jpeg"))).toBeNull();
    expect(uploadProblem(f("a.png", "image/png"))).toBeNull();
    expect(uploadProblem(f("IMG_1.HEIC", ""))).toBeNull();
    expect(uploadProblem(f("a.heic", "image/heic"))).toBeNull();
    expect(uploadProblem(f("a.pdf", "application/pdf"))).toBeNull();
  });
  it("rejects other types, empty and oversized files", () => {
    expect(uploadProblem(f("a.docx", "application/vnd.openxmlformats"))).toMatch(/only photos/);
    expect(uploadProblem(f("a.exe", "application/octet-stream"))).toMatch(/only photos/);
    expect(uploadProblem(f("a.pdf", "application/pdf", 10 * 1024 * 1024 + 1))).toMatch(/10 MB/);
    expect(uploadProblem(f("a.pdf", "application/pdf", 0))).toMatch(/empty/);
    const { ok, rejected } = splitUploads([f("a.pdf", "application/pdf"), f("b.gif", "image/gif")]);
    expect(ok).toHaveLength(1);
    expect(rejected[0]?.filename).toBe("b.gif");
  });
});

describe("labels", () => {
  it("formats metrics and badges in plain English", () => {
    expect(formatMetric("field_f1", 0.97)).toBe("97%");
    expect(formatMetric("stp", 0.764)).toBe("76.4%");
    expect(formatMetric("usd_per_doc", "0.011")).toBe("$0.011");
    expect(formatMetric("p50_s", 4.1)).toBe("4.1 s");
    expect(formatMetric("ece", 0.0314)).toBe("0.031");
    expect(badgeInfo("bank_change")).toEqual({ label: "Bank change", tone: "red" });
    expect(badgeInfo("work_order").label).toBe("Work order issue");
    expect(badgeInfo("something_new").label).toBe("something new");
  });
});
