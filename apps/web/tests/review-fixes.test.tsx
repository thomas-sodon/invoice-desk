import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiProvider } from "@/components/api-provider";
import { batchCostFromEvent, upsertCard } from "@/components/board/use-board";
import { ApprovalPanel } from "@/components/viewer/approval-panel";
import { FollowupBox } from "@/components/viewer/followup-box";
import { RulesList } from "@/components/viewer/rules-list";
import { ApiError, setApiForTests } from "@/lib/api";
import type { BoardEvent, DocumentCard, DocumentDetail } from "@/lib/api-types";
import { approvalProgress } from "@/lib/approvals";
import { setDevUser } from "@/lib/auth";
import { maskFieldValue } from "@/lib/mask";
import fixtures from "@/lib/mock/fixtures/demo-batch.json";
import { MockApiClient } from "@/lib/mock/server";

const docs = (fixtures as unknown as { documents: DocumentDetail[] }).documents;
const byPrefix = (p: string) => docs.find((d) => d.filename.startsWith(p)) as DocumentDetail;

const CLERK = "dana.clerk@maplerow.example";
const APPROVER = "priya.approver@maplerow.example";
const OWNER = "sam.owner@maplerow.example";

afterEach(() => {
  cleanup();
  setApiForTests(null);
});

describe("board card refresh ordering", () => {
  const card = (status: DocumentCard["status"], updated_at: string): DocumentCard => ({
    ...byPrefix("01-"),
    id: "d1",
    status,
    column: status === "Flagged" ? "flagged" : "received",
    updated_at,
  });

  it("an older re-fetch never moves a card back", () => {
    const now = [card("Flagged", "2026-10-24T10:00:02Z")];
    expect(upsertCard(now, card("Screening", "2026-10-24T10:00:01Z"))).toBe(now);
  });

  it("a newer or equal re-fetch replaces the card, an unknown one is appended", () => {
    const now = [card("Screening", "2026-10-24T10:00:01Z")];
    expect(upsertCard(now, card("Flagged", "2026-10-24T10:00:01Z"))[0]?.status).toBe("Flagged");
    expect(upsertCard(now, { ...card("Flagged", "2026-10-24T10:00:03Z"), id: "d2" })).toHaveLength(2);
  });

  it("only a batch view takes the batch cost from events", () => {
    const ev = { batch_id: "b1", batch_cost_usd: "0.27" } as BoardEvent;
    expect(batchCostFromEvent(null, ev)).toBeNull();
    expect(batchCostFromEvent("b2", ev)).toBeNull();
    expect(batchCostFromEvent("b1", ev)).toBe("0.27");
    expect(batchCostFromEvent("b1", { ...ev, batch_cost_usd: null })).toBeNull();
  });
});

describe("field masking", () => {
  it("keeps invoice numbers, dates and amounts, masks bank details and free text", () => {
    expect(maskFieldValue("invoice_number", "26-7200")).toBe("26-7200");
    expect(maskFieldValue("invoice_date", "2026-10-06")).toBe("2026-10-06");
    expect(maskFieldValue("remit_to_bank_details", "Acct 12345 routing 021000021")).toBe("Acct ••••2345 routing ••••0021");
    expect(maskFieldValue("vendor_address", "Ref 000555067890")).toBe("Ref ••••7890");
  });

  it("rule messages are masked before display", () => {
    render(
      <RulesList
        rules={[{ rule_id: "R6", name: "bank_change", passed: false, severity: "flag", message: "Remit-to changed to 000555067890", fields: [], details: {} }]}
      />,
    );
    expect(document.body.textContent).toContain("••••7890");
    expect(document.body.textContent).not.toMatch(/\d{6,}/);
  });
});

describe("mock API parity with the state machine", () => {
  let api: MockApiClient;
  beforeEach(() => {
    vi.useFakeTimers();
    setDevUser(CLERK);
    api = new MockApiClient({ stepMs: 10, persist: false, latencyMs: 0 });
  });
  afterEach(() => vi.useRealTimers());

  const idOf = async (prefix: string) => (await api.listDocuments()).items.find((d) => d.filename.startsWith(prefix))?.id as string;

  it("a reject vote blocks release even with two approvals and a callback", async () => {
    const id = await idOf("21-");
    setDevUser(APPROVER);
    await api.approve(id, { kind: "duplicate", decision: "reject", callback_attested: false });
    setDevUser(OWNER);
    const r = await api.approve(id, { kind: "duplicate", decision: "approve", callback_attested: true });
    expect(r.status).toBe("Flagged");
    expect(approvalProgress(await api.getDocument(id)).rejected).toBe(true);
  });

  it("a released document with nothing read cannot be posted", async () => {
    const id = await idOf("18-");
    setDevUser(APPROVER);
    await api.approve(id, { kind: "bank_change", decision: "approve", callback_attested: true });
    setDevUser(OWNER);
    expect((await api.approve(id, { kind: "bank_change", decision: "approve", callback_attested: true })).status).toBe("ReadyToPay");
    await expect(api.post(id)).rejects.toBeInstanceOf(ApiError);
    const ready = await api.postReady(api.latestBatchId() as string);
    expect(ready.count).toBe(18);
  });

  it("a drafted follow-up appears on the document", async () => {
    const id = await idOf("10-");
    const f = await api.createFollowup(id);
    expect((await api.getDocument(id)).followups.map((x) => x.id)).toContain(f.id);
  });
});

describe("per-document panel state", () => {
  async function renderWith(node: (doc: DocumentDetail) => React.ReactElement, first: DocumentDetail) {
    setApiForTests(new MockApiClient({ persist: false, latencyMs: 0 }));
    let rerender: (ui: React.ReactElement) => void = () => undefined;
    await act(async () => {
      rerender = render(<ApiProvider>{node(first)}</ApiProvider>).rerender;
    });
    return (doc: DocumentDetail) => rerender(<ApiProvider>{node(doc)}</ApiProvider>);
  }

  it("a ticked callback box does not carry over to the next invoice", async () => {
    setDevUser(OWNER);
    const swap = await renderWith((doc) => <ApprovalPanel doc={doc} onDone={() => undefined} />, byPrefix("18-"));
    const box = await screen.findByRole("checkbox");
    await act(async () => box.click());
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
    await act(async () => swap(byPrefix("13-")));
    await waitFor(() => expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false));
  });

  it("a follow-up draft for one invoice is not shown under the next", async () => {
    const first = { ...byPrefix("10-"), followups: [{ id: "f1", document_id: "x", to_addr: null, draft_subject: "About invoice INV-2240", draft_body: "Hi", approved_by: null, sent_at: null }] };
    const swap = await renderWith((doc) => <FollowupBox doc={doc} />, first);
    expect(screen.getByText("About invoice INV-2240")).toBeTruthy();
    await act(async () => swap({ ...byPrefix("03-"), followups: [] }));
    expect(screen.queryByText("About invoice INV-2240")).toBeNull();
    expect(screen.getByRole("button", { name: /Draft an email/ })).toBeTruthy();
  });
});
