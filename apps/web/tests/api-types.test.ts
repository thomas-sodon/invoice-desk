import { describe, expect, it } from "vitest";
import {
  isApiErrorBody,
  isBoardEvent,
  isCents,
  isDocStatus,
  isDocumentCard,
  isDocumentDetail,
  isKanbanColumn,
  isReplayUpload,
  isUsdString,
  normalizeBoardEvent,
} from "@/lib/api-types";
import { MOCK_STAFF } from "@/lib/mock/server";
import fixtures from "@/lib/mock/fixtures/demo-batch.json";

const event = {
  type: "document.status_changed",
  tenant_id: "maple_row",
  document_id: "3c0c7c0e-0000-4000-8000-000000000001",
  batch_id: null,
  status: "Flagged",
  column: "flagged",
  cost_usd: "0.0009",
  batch_cost_usd: "0.27",
  message: "Duplicate of INV-2231",
  ts: "2026-10-24T14:20:00.123+00:00",
};

describe("primitive guards", () => {
  it("accepts integer cents only", () => {
    expect(isCents(124000)).toBe(true);
    expect(isCents(-5)).toBe(true);
    expect(isCents(1240.5)).toBe(false);
    expect(isCents("124000")).toBe(false);
    expect(isCents(Number.NaN)).toBe(false);
  });
  it("accepts USD decimal strings, never floats", () => {
    expect(isUsdString("0.0112")).toBe(true);
    expect(isUsdString("12")).toBe(true);
    expect(isUsdString(0.0112)).toBe(false);
    expect(isUsdString("1e-3")).toBe(false);
    expect(isUsdString("$1")).toBe(false);
  });
  it("knows the status and column unions", () => {
    expect(isDocStatus("ReadyToPay")).toBe(true);
    expect(isDocStatus("ready_to_pay")).toBe(false);
    expect(isKanbanColumn("needs_review")).toBe(true);
    expect(isKanbanColumn("NeedsReview")).toBe(false);
  });
});

describe("BoardEvent guard", () => {
  it("accepts the contract example", () => {
    expect(isBoardEvent(event)).toBe(true);
  });
  it("accepts omitted nullable keys and normalizes them to null", () => {
    const minimal = { type: "batch.completed", tenant_id: "maple_row", ts: event.ts };
    expect(isBoardEvent(minimal)).toBe(true);
    if (isBoardEvent(minimal)) expect(normalizeBoardEvent(minimal).document_id).toBeNull();
  });
  it("rejects bad shapes", () => {
    expect(isBoardEvent({ ...event, type: "document.deleted" })).toBe(false);
    expect(isBoardEvent({ ...event, status: "Paid" })).toBe(false);
    expect(isBoardEvent({ ...event, column: "Flagged" })).toBe(false);
    expect(isBoardEvent({ ...event, cost_usd: 0.0009 })).toBe(false);
    expect(isBoardEvent({ ...event, tenant_id: undefined })).toBe(false);
    expect(isBoardEvent(null)).toBe(false);
    expect(isBoardEvent([])).toBe(false);
  });
});

describe("document guards", () => {
  const docs = (fixtures as unknown as { documents: unknown[] }).documents;
  it("every fixture document is a valid DocumentDetail", () => {
    expect(docs).toHaveLength(25);
    for (const d of docs) {
      expect(isDocumentCard(d)).toBe(true);
      expect(isDocumentDetail(d)).toBe(true);
    }
  });
  it("rejects float money and unknown statuses", () => {
    const d = docs[0] as Record<string, unknown>;
    expect(isDocumentCard({ ...d, total_cents: 1240.5 })).toBe(false);
    expect(isDocumentCard({ ...d, status: "Paid" })).toBe(false);
    expect(isDocumentCard({ ...d, cost_usd: 0.01 })).toBe(false);
    expect(isDocumentDetail({ ...d, fields: [{ name: "total", value: "1", band: "blue" }] })).toBe(false);
  });
  it("fixture board matches the demo script: 18 / 4 / 3", () => {
    const counts: Record<string, number> = {};
    for (const d of docs as { status: string }[]) counts[d.status] = (counts[d.status] ?? 0) + 1;
    expect(counts).toEqual({ ReadyToPay: 18, NeedsReview: 4, Flagged: 3 });
  });
  it("fixtures never carry a full bank or account number", () => {
    const text = JSON.stringify(docs).replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, "");
    const strings = text.match(/"(?:[^"\\]|\\.)*"/g) ?? [];
    const offenders = strings.filter((s) => !/^"-?\d+"$/.test(s) && /\d{6,}/.test(s));
    expect(offenders).toEqual([]);
  });
  it("staff fixtures cover the three roles", () => {
    expect(MOCK_STAFF.map((s) => s.role)).toEqual(["clerk", "approver", "owner"]);
  });
});

describe("misc guards", () => {
  it("recognizes error bodies and replay uploads", () => {
    expect(isApiErrorBody({ error: { code: "self_approval", message: "no" } })).toBe(true);
    expect(isApiErrorBody({ error: "nope" })).toBe(false);
    expect(isReplayUpload({ replay: true, replay_batch_id: "b" })).toBe(true);
    expect(isReplayUpload({ document_id: "d" })).toBe(false);
  });
});
