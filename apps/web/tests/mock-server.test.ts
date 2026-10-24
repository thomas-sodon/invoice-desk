import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { BoardEvent, DocumentCard } from "@/lib/api-types";
import { setDevUser } from "@/lib/auth";
import { MockApiClient } from "@/lib/mock/server";
import { applyBoardEvent } from "@/components/board/use-board";

const CLERK = "dana.clerk@maplerow.example";
const APPROVER = "priya.approver@maplerow.example";
const OWNER = "sam.owner@maplerow.example";

async function find(api: MockApiClient, prefix: string) {
  const { items } = await api.listDocuments();
  const card = items.find((d) => d.filename.startsWith(prefix));
  if (!card) throw new Error(`no ${prefix}`);
  return card;
}

async function expectApiError(p: Promise<unknown>, code: string) {
  await expect(p).rejects.toBeInstanceOf(ApiError);
  await p.catch((e: ApiError) => expect(e.code).toBe(code));
}

describe("mock API", () => {
  let api: MockApiClient;
  beforeEach(() => {
    vi.useFakeTimers();
    setDevUser(CLERK);
    api = new MockApiClient({ stepMs: 10, persist: false, latencyMs: 0 });
  });
  afterEach(() => vi.useRealTimers());

  it("starts with the settled demo batch", async () => {
    const id = api.latestBatchId();
    expect(id).toBeTruthy();
    const b = await api.getBatch(id as string);
    expect(b.counts).toEqual({ ready_to_pay: 18, needs_review: 4, flagged: 3 });
    expect(b.cost_usd).toBe("0.2705");
  });

  it("replays the demo with live events that end 18 / 4 / 3", async () => {
    const events: BoardEvent[] = [];
    api.subscribe(null, (e) => events.push(e));
    const run = await api.runDemo("replay");
    expect(run.document_ids).toHaveLength(25);
    await vi.runAllTimersAsync();
    const b = await api.getBatch(run.batch_id);
    expect(b.counts).toEqual({ ready_to_pay: 18, needs_review: 4, flagged: 3 });
    const statuses = events.filter((e) => e.type === "document.status_changed").map((e) => e.status);
    expect(statuses).toContain("Extracted");
    expect(statuses).toContain("Validating");
    expect(statuses).toContain("Quarantined");
    expect(events.at(-1)?.type).toBe("batch.completed");
    expect(events.at(-1)?.batch_cost_usd).toBe("0.2705");
    // Quarantined documents never reach Extracting.
    const quarantinedIds = new Set(events.filter((e) => e.status === "Quarantined").map((e) => e.document_id));
    expect(events.some((e) => quarantinedIds.has(e.document_id) && e.status === "Extracting")).toBe(false);
  });

  it("enforces the two-person rule with a callback, never the uploader", async () => {
    const card = await find(api, "18-");
    // the clerk who uploaded it cannot approve
    await expectApiError(api.approve(card.id, { kind: "bank_change", decision: "approve", callback_attested: true }), "forbidden");
    setDevUser(APPROVER);
    await expectApiError(api.approve(card.id, { kind: "bank_change", decision: "approve", callback_attested: false }), "callback_required");
    let r = await api.approve(card.id, { kind: "bank_change", decision: "approve", callback_attested: true });
    expect(r.status).toBe("Flagged");
    await expectApiError(api.approve(card.id, { kind: "bank_change", decision: "approve", callback_attested: true }), "duplicate_approval");
    setDevUser(OWNER);
    r = await api.approve(card.id, { kind: "bank_change", decision: "approve", callback_attested: true });
    expect(r.status).toBe("ReadyToPay");
    expect(r.approvals).toHaveLength(2);
  });

  it("refuses self-approval for an approver who uploaded the batch", async () => {
    setDevUser(APPROVER);
    const run = await api.runDemo("replay");
    await vi.runAllTimersAsync();
    const flagged = (await api.getBatch(run.batch_id)).documents.find((d) => d.status === "Flagged") as DocumentCard;
    await expectApiError(api.approve(flagged.id, { kind: "bank_change", decision: "approve", callback_attested: true }), "self_approval");
  });

  it("corrects an unreadable total and re-routes to ready to pay", async () => {
    const card = await find(api, "03-");
    const before = await api.getDocument(card.id);
    expect(before.status).toBe("NeedsReview");
    const r = await api.correct(card.id, [{ field: "total", value: "51000" }]);
    expect(r.status).toBe("ReadyToPay");
    const after = await api.getDocument(card.id);
    expect(after.total_cents).toBe(51000);
    expect(after.fields.find((f) => f.name === "total")?.band).toBe("green");
    const audit = await api.getAudit(card.id);
    expect(audit.entries.some((e) => e.action === "review.field_corrected")).toBe(true);
    expect(audit.chain_ok).toBe(true);
  });

  it("keeps an over-estimate invoice in review when the total still exceeds the estimate", async () => {
    const card = await find(api, "10-");
    const r = await api.correct(card.id, [{ field: "total", value: String(card.total_cents) }]);
    expect(r.status).toBe("NeedsReview");
    await expectApiError(api.correct((await find(api, "01-")).id, [{ field: "total", value: "1" }]), "illegal_transition");
  });

  it("posts only ready-to-pay invoices, idempotently by state", async () => {
    const flagged = await find(api, "21-");
    await expectApiError(api.post(flagged.id), "illegal_transition");
    const ready = await api.postReady(api.latestBatchId() as string);
    expect(ready.count).toBe(18);
    await vi.runAllTimersAsync();
    const b = await api.getBatch(api.latestBatchId() as string);
    expect(b.counts.posted).toBe(18);
    const posted = b.documents.find((d) => d.status === "Posted") as DocumentCard;
    expect((await api.getDocument(posted.id)).bill?.status).toBe("posted");
    await expectApiError(api.post(posted.id), "illegal_transition");
  });

  it("rejects held invoices and drafts follow-ups that need approval to send", async () => {
    const dup = await find(api, "21-");
    expect((await api.reject(dup.id, "duplicate")).status).toBe("Rejected");
    const over = await find(api, "10-");
    const draft = await api.createFollowup(over.id);
    expect(draft.approved_by).toBeNull();
    expect((await api.createFollowup(over.id)).id).toBe(draft.id);
    const sent = await api.approveFollowup(draft.id, { subject: "Edited" });
    expect(sent.draft_subject).toBe("Edited");
    expect(sent.sent_at).toBeTruthy();
    await expectApiError(api.approveFollowup(draft.id), "already_approved");
  });

  it("approves a new vendor with one non-uploader approval", async () => {
    const card = await find(api, "11-");
    setDevUser(APPROVER);
    const r = await api.approve(card.id, { kind: "new_vendor", decision: "approve", callback_attested: false });
    expect(r.status).toBe("ReadyToPay");
  });

  it("try-it: needs the human check, never posts, hides bank details", async () => {
    await expectApiError(api.tryitUpload(new File(["x"], "a.png", { type: "image/png" })), "turnstile_failed");
    await expectApiError(api.tryitSession(""), "turnstile_failed");
    const s = await api.tryitSession("tok");
    expect(s.uploads_remaining).toBe(3);
    const up = await api.tryitUpload(new File(["x"], "a.png", { type: "image/png" }));
    if (!("document_id" in up)) throw new Error("expected a document");
    await vi.runAllTimersAsync();
    const d = await api.tryitDocument(up.document_id);
    expect(d.status).toBe("NeedsReview");
    expect(d.original_url).toBeNull();
    expect(d.fields.some((f) => f.name === "remit_to_bank_details")).toBe(false);
    await expectApiError(api.post(up.document_id), "forbidden");
    await expectApiError(api.getDocument(up.document_id), "not_found");
    await api.tryitUpload(new File(["x"], "b.png", { type: "image/png" }));
    await api.tryitUpload(new File(["x"], "c.png", { type: "image/png" }));
    await expectApiError(api.tryitUpload(new File(["x"], "d.png", { type: "image/png" })), "rate_limited");
  });

  it("staff uploads go through the board without inventing values", async () => {
    const r = await api.uploadDocuments([
      new File(["%PDF-1.4"], "scan.pdf", { type: "application/pdf" }),
      new File(["x"], "notes.txt", { type: "text/plain" }),
    ]);
    expect(r.documents).toHaveLength(1);
    expect(r.rejected[0]?.filename).toBe("notes.txt");
    await vi.runAllTimersAsync();
    const d = await api.getDocument(r.documents[0]?.id as string);
    expect(d.status).toBe("NeedsReview");
    expect(d.fields).toEqual([]);
    expect(d.redactions).toEqual([]);
    expect(d.headline).toMatch(/Offline preview/);
    expect(d.uploaded_by).toBe((await api.me())?.id);
  });

  it("comparison shows WRONG for the naive pipeline only", async () => {
    const c = await api.comparison(api.latestBatchId() as string);
    expect(c.rows).toHaveLength(25);
    expect(c.summary.invoicedesk_wrong).toBe(0);
    expect(c.summary.naive_wrong).toBeGreaterThanOrEqual(2);
    const dup = c.rows.find((r) => r.filename.startsWith("21-"));
    expect(dup?.naive.reason).toBe("Posted duplicate $1,240.00");
    expect(c.rows.find((r) => r.filename.startsWith("18-"))?.naive.reason).toBe("Paid to attacker's account");
  });

  it("dashboard and scorecard carry the synthetic-data label", async () => {
    expect((await api.dashboard()).label).toBe("benchmark on synthetic data");
    expect((await api.scorecard())?.label).toBe("benchmark on synthetic data");
  });
});

describe("board reducer", () => {
  const card: DocumentCard = {
    id: "d1",
    batch_id: "b",
    filename: "f.pdf",
    source: "upload",
    status: "Extracted",
    column: "extracted",
    vendor_name: null,
    invoice_number: null,
    total_cents: null,
    headline: null,
    badges: [],
    cost_usd: "0.001",
    thumbnail_url: null,
    created_at: "",
    updated_at: "",
  };
  const ev = (over: Partial<BoardEvent>): BoardEvent => ({
    type: "document.status_changed",
    tenant_id: "maple_row",
    document_id: "d1",
    batch_id: "b",
    status: null,
    column: null,
    cost_usd: null,
    batch_cost_usd: null,
    message: null,
    ts: "t",
    ...over,
  });
  it("moves the card to the event's column", () => {
    const [c] = applyBoardEvent([card], ev({ status: "Flagged", column: "flagged", cost_usd: "0.002" }));
    expect(c).toMatchObject({ status: "Flagged", column: "flagged", cost_usd: "0.002" });
  });
  it("derives the column when the event omits it", () => {
    const [c] = applyBoardEvent([card], ev({ status: "Quarantined" }));
    expect(c?.column).toBe("flagged");
  });
  it("ignores events for unknown documents", () => {
    const cards = [card];
    expect(applyBoardEvent(cards, ev({ document_id: "other", status: "Posted" }))).toBe(cards);
    expect(applyBoardEvent(cards, ev({ document_id: null }))).toBe(cards);
  });
});
