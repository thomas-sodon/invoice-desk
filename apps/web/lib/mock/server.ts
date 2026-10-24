/**
 * Mock API for offline UI work and the screenshot script (NEXT_PUBLIC_API_MOCK=1).
 *
 * It replays the synthetic 25-invoice demo batch from fixtures and simulates the server's
 * behaviour closely enough to exercise every screen: realtime card movement, corrections,
 * two-person approvals (uploader refused, callback required), posting, follow-ups and try-it.
 * The decisions here are scripted from the fixture's expected outcomes; the real decisions are
 * made by the Python rules engine behind the real API.
 */
import { ApiError, type ApiClient, type ListParams, type Subscription, type ConnectionState } from "../api";
import type {
  Approval,
  ApprovalRequest,
  ApprovalResponse,
  AuditEntry,
  AuditView,
  BatchInfo,
  BatchView,
  BoardEvent,
  Comparison,
  Correction,
  Dashboard,
  DemoRunResponse,
  DocStatus,
  DocumentDetail,
  DocumentList,
  EventType,
  Field,
  Followup,
  KanbanColumn,
  Me,
  RuleResult,
  Scorecard,
  TryitSession,
  TryitStatus,
  TryitUploadResponse,
  UploadResponse,
} from "../api-types";
import { getDevUser } from "../auth";
import { columnFor } from "../columns";
import { addUsd, formatCents } from "../money";
import { isPdfName, splitUploads } from "../upload";
import fixtures from "./fixtures/demo-batch.json";

export interface MockHints {
  truth_total_cents: number | null;
  truth_vendor: string | null;
  vendor_email?: string | null;
  medium: string;
  failing: Record<string, string>;
  illegible: string[];
}

export type MockDoc = DocumentDetail & { _mock: MockHints; session_id?: string | null };

interface Fixtures {
  batch: BatchInfo;
  tenant_id: string;
  staff: Me[];
  documents: MockDoc[];
  comparison: Comparison;
  dashboard: Dashboard;
  scorecard: Scorecard;
  followups: Followup[];
}

const FIX = fixtures as unknown as Fixtures;
export const MOCK_STAFF: readonly Me[] = FIX.staff;
const TENANT = FIX.tenant_id;
const STORAGE_KEY = "invoicedesk.mock.v1";

interface MockState {
  batches: BatchInfo[];
  docs: Record<string, MockDoc>;
  order: string[];
  followups: Followup[];
  audit: Record<string, AuditEntry[]>;
  tryit: { session_id: string | null; uploads_remaining: number };
  seq: number;
}

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

function nowIso(): string {
  return new Date().toISOString();
}

function uuid(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-8xxx-xxxxxxxxxxxx".replace(/x/g, () => Math.floor(Math.random() * 16).toString(16));
}

/** Tiny non-cryptographic hash for the mock audit chain display (the real chain is SHA-256). */
function mockHash(s: string): string {
  let h1 = 0x811c9dc5;
  let h2 = 0x01000193;
  for (let i = 0; i < s.length; i++) {
    h1 = Math.imul(h1 ^ s.charCodeAt(i), 16777619);
    h2 = Math.imul(h2 ^ s.charCodeAt(i), 2246822519);
  }
  return ((h1 >>> 0).toString(16).padStart(8, "0") + (h2 >>> 0).toString(16).padStart(8, "0")).repeat(4);
}

function initialState(): MockState {
  const docs: Record<string, MockDoc> = {};
  const order: string[] = [];
  for (const d of FIX.documents) {
    docs[d.id] = clone(d);
    order.push(d.id);
  }
  const audit: Record<string, AuditEntry[]> = {};
  const state: MockState = {
    batches: [clone(FIX.batch)],
    docs,
    order,
    followups: clone(FIX.followups),
    audit,
    tryit: { session_id: null, uploads_remaining: 3 },
    seq: 0,
  };
  for (const d of FIX.documents) {
    for (const [action, after] of seedAuditTrail(d)) appendAudit(state, d.id, "system", action, null, after, d.created_at);
  }
  return state;
}

function seedAuditTrail(d: MockDoc): [string, unknown][] {
  const trail: [string, unknown][] = [
    ["document.created", { filename: d.filename }],
    ["status.changed", { status: "Screening" }],
  ];
  for (const s of d.screen_results) trail.push([`screen.${s.detector}`, { verdict: s.verdict, categories: s.categories }]);
  if (d.status === "Flagged" && d.fields.length === 0) {
    trail.push(["status.changed", { status: "Quarantined" }], ["status.changed", { status: "Flagged" }]);
    return trail;
  }
  trail.push(["status.changed", { status: "Extracting" }], ["status.changed", { status: "Extracted" }]);
  trail.push(["status.changed", { status: "Validating" }]);
  for (const r of d.rule_results) trail.push([`rule.${r.rule_id}`, { severity: r.severity, passed: r.passed }]);
  trail.push(["status.changed", { status: d.status }]);
  return trail;
}

function appendAudit(state: MockState, docId: string, actor: string, action: string, before: unknown, after: unknown, ts = nowIso()): void {
  const list = (state.audit[docId] ??= []);
  state.seq += 1;
  const prev = list.at(-1)?.row_hash ?? "0".repeat(64);
  const entry: AuditEntry = { id: state.seq, ts, actor, action, before, after, row_hash: "" };
  entry.row_hash = mockHash(prev + JSON.stringify({ ...entry, row_hash: undefined }));
  list.push(entry);
}

function toCard(d: MockDoc) {
  return {
    id: d.id,
    batch_id: d.batch_id,
    filename: d.filename,
    source: d.source,
    status: d.status,
    column: d.column,
    vendor_name: d.vendor_name,
    invoice_number: d.invoice_number,
    total_cents: d.total_cents,
    headline: d.headline,
    badges: d.badges,
    cost_usd: d.cost_usd,
    thumbnail_url: d.thumbnail_url,
    created_at: d.created_at,
    updated_at: d.updated_at,
  };
}

function toDetail(d: MockDoc): DocumentDetail {
  const { _mock: _hints, session_id: _sid, ...rest } = d;
  return clone(rest);
}

function failingRules(d: MockDoc): RuleResult[] {
  return d.rule_results.filter((r) => !r.passed);
}

export interface MockOptions {
  /** Milliseconds between pipeline steps. */
  stepMs?: number;
  /** Persist state in sessionStorage so client navigation and reloads keep it. */
  persist?: boolean;
  /** Simulated network latency for reads (0 = resolve synchronously; used by tests). */
  latencyMs?: number;
}

export class MockApiClient implements ApiClient {
  readonly mode = "mock" as const;
  private state: MockState;
  private listeners = new Set<{ batchId: string | null; fn: (e: BoardEvent) => void }>();
  private readonly stepMs: number;
  private readonly persist: boolean;
  private readonly latencyMs: number;
  private blobUrls = new Map<string, string>();

  constructor(opts: MockOptions = {}) {
    this.stepMs = opts.stepMs ?? 420;
    this.persist = opts.persist ?? typeof window !== "undefined";
    this.latencyMs = opts.latencyMs ?? 30;
    this.state = this.load() ?? initialState();
    this.resumeInterrupted();
  }

  // ------------------------------------------------------------------ persistence and events

  private load(): MockState | null {
    if (!this.persist) return null;
    try {
      const raw = window.sessionStorage.getItem(STORAGE_KEY);
      return raw ? (JSON.parse(raw) as MockState) : null;
    } catch {
      return null;
    }
  }

  private save(): void {
    if (!this.persist) return;
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(this.state));
    } catch {
      /* quota or privacy mode: state stays in memory for this tab */
    }
  }

  /** Clears mock state back to the settled demo batch (used by tests and the "Reset" link). */
  reset(): void {
    this.state = initialState();
    this.save();
  }

  private resumeInterrupted(): void {
    // A reload in the middle of a simulated run would leave cards mid-pipeline; finish them.
    for (const d of Object.values(this.state.docs)) {
      if (["Received", "Screening", "Extracting", "Extracted", "Validating"].includes(d.status) && d._mock.medium !== "upload") {
        const tpl = FIX.documents.find((f) => f.filename === d.filename);
        if (tpl) this.applyFinal(d, tpl);
      }
    }
  }

  private emit(type: EventType, d: MockDoc | null, batchId: string | null, message: string | null = null): void {
    const ev: BoardEvent = {
      type,
      tenant_id: TENANT,
      document_id: d?.id ?? null,
      batch_id: batchId,
      status: d?.status ?? null,
      column: d?.column ?? null,
      cost_usd: d?.cost_usd ?? null,
      batch_cost_usd: batchId ? this.batchCost(batchId) : null,
      message: message ?? d?.headline ?? null,
      ts: nowIso(),
    };
    for (const l of this.listeners) if (l.batchId === null || l.batchId === batchId) l.fn(ev);
  }

  private batchCost(batchId: string): string {
    return addUsd(...this.state.order.map((id) => this.state.docs[id]).filter((d) => d?.batch_id === batchId).map((d) => d?.cost_usd));
  }

  private setStatus(d: MockDoc, status: DocStatus, actor = "system"): void {
    const before = d.status;
    d.status = status;
    d.column = columnFor(status);
    d.updated_at = nowIso();
    appendAudit(this.state, d.id, actor, "status.changed", { status: before }, { status });
    this.emit("document.status_changed", d, d.batch_id);
  }

  private me_(): Me {
    const email = getDevUser();
    return MOCK_STAFF.find((s) => s.email === email) ?? (MOCK_STAFF[0] as Me);
  }

  private doc(id: string): MockDoc {
    const d = this.state.docs[id];
    if (!d) throw new ApiError(404, "not_found", "That invoice doesn't exist (or was deleted).");
    return d;
  }

  private delay(ms = this.latencyMs): Promise<void> {
    if (ms <= 0) return Promise.resolve();
    return new Promise((r) => setTimeout(r, ms));
  }

  // ------------------------------------------------------------------ identity

  async me(): Promise<Me | null> {
    return this.me_();
  }

  // ------------------------------------------------------------------ ingest

  async createBatch(label: string): Promise<{ batch_id: string }> {
    const b: BatchInfo = { id: uuid(), label, mode: "live", created_at: nowIso() };
    this.state.batches.push(b);
    this.save();
    return { batch_id: b.id };
  }

  async runDemo(_mode: "live" | "replay"): Promise<DemoRunResponse> {
    const batch: BatchInfo = { id: uuid(), label: "25-invoice demo batch", mode: "replay", created_at: nowIso() };
    this.state.batches.push(batch);
    const me = this.me_();
    const ids: string[] = [];
    for (const tpl of FIX.documents) {
      const d = this.blank(tpl, batch.id, me.id);
      this.state.docs[d.id] = d;
      this.state.order.push(d.id);
      ids.push(d.id);
    }
    this.save();
    ids.forEach((id, i) => {
      const d = this.state.docs[id] as MockDoc;
      const tpl = FIX.documents[i] as MockDoc;
      setTimeout(() => {
        appendAudit(this.state, d.id, `user:${me.id}`, "document.created", null, { filename: d.filename });
        this.emit("document.created", d, batch.id);
      }, i * (this.stepMs / 3));
      this.schedulePipeline(d, tpl, i * (this.stepMs / 3) + this.stepMs);
    });
    setTimeout(
      () => {
        this.emit("batch.completed", null, batch.id, "Batch finished");
        this.save();
      },
      ids.length * (this.stepMs / 3) + this.stepMs * 6,
    );
    return { batch_id: batch.id, document_ids: ids };
  }

  private blank(tpl: MockDoc, batchId: string, uploaderId: string): MockDoc {
    const d = clone(tpl);
    const ts = nowIso();
    Object.assign(d, {
      id: uuid(),
      batch_id: batchId,
      status: "Received" as DocStatus,
      column: "received" as KanbanColumn,
      vendor_name: null,
      invoice_number: null,
      total_cents: null,
      headline: null,
      badges: [],
      cost_usd: "0",
      fields: [],
      line_items: [],
      rule_results: [],
      screen_results: [],
      embedded_instructions: [],
      extractions: [],
      approvals: [],
      approvals_required: null,
      bill: null,
      followups: [],
      created_at: ts,
      updated_at: ts,
      uploaded_by: uploaderId,
    });
    return d;
  }

  private schedulePipeline(d: MockDoc, tpl: MockDoc, start: number): void {
    const quarantined = tpl.fields.length === 0 && tpl.status === "Flagged";
    const step = this.stepMs;
    setTimeout(() => {
      this.setStatus(d, "Screening");
      d.screen_results = clone(tpl.screen_results);
      d.cost_usd = quarantined ? tpl.cost_usd : "0.0011";
      this.emit("batch.cost_updated", d, d.batch_id);
    }, start);
    if (quarantined) {
      setTimeout(() => {
        d.embedded_instructions = clone(tpl.embedded_instructions);
        d.extractions = clone(tpl.extractions);
        this.setStatus(d, "Quarantined");
      }, start + step);
      setTimeout(() => this.applyFinal(d, tpl, true), start + step * 2);
      return;
    }
    setTimeout(() => this.setStatus(d, "Extracting"), start + step);
    setTimeout(() => {
      d.fields = clone(tpl.fields);
      d.line_items = clone(tpl.line_items);
      d.extractions = clone(tpl.extractions);
      d.vendor_name = tpl.vendor_name;
      d.invoice_number = tpl.invoice_number;
      d.total_cents = tpl.total_cents;
      d.cost_usd = tpl.cost_usd;
      this.setStatus(d, "Extracted");
      this.emit("batch.cost_updated", d, d.batch_id);
    }, start + step * 2);
    setTimeout(() => this.setStatus(d, "Validating"), start + step * 3);
    setTimeout(() => this.applyFinal(d, tpl, true), start + step * 4);
  }

  private applyFinal(d: MockDoc, tpl: MockDoc, emit = false): void {
    Object.assign(d, {
      fields: clone(tpl.fields),
      line_items: clone(tpl.line_items),
      rule_results: clone(tpl.rule_results),
      screen_results: clone(tpl.screen_results),
      embedded_instructions: clone(tpl.embedded_instructions),
      extractions: clone(tpl.extractions),
      vendor_name: tpl.vendor_name,
      invoice_number: tpl.invoice_number,
      total_cents: tpl.total_cents,
      headline: tpl.headline,
      badges: clone(tpl.badges),
      cost_usd: tpl.cost_usd,
      approvals_required: clone(tpl.approvals_required),
    });
    for (const r of d.rule_results) appendAudit(this.state, d.id, "system", `rule.${r.rule_id}`, null, { severity: r.severity });
    if (emit) this.setStatus(d, tpl.status);
    else {
      d.status = tpl.status;
      d.column = columnFor(tpl.status);
    }
    this.save();
  }

  async uploadDocuments(files: File[], batchId?: string): Promise<UploadResponse> {
    const { ok, rejected } = splitUploads(files);
    const bid = batchId ?? (await this.createBatch(`Upload ${new Date().toLocaleDateString("en-US")}`)).batch_id;
    const me = this.me_();
    const documents = ok.map((f) => {
      const d = this.uploadedDoc(f, bid, me.id, null);
      return { id: d.id, filename: d.filename, status: d.status };
    });
    this.save();
    return { batch_id: bid, documents, rejected };
  }

  private uploadedDoc(file: File, batchId: string | null, uploaderId: string | null, sessionId: string | null): MockDoc {
    const tpl = FIX.documents[0] as MockDoc;
    const d = this.blank(tpl, batchId ?? "", uploaderId ?? "");
    d.batch_id = batchId;
    d.filename = file.name;
    d.thumbnail_url = null;
    d.session_id = sessionId;
    d.source = "upload";
    d.redactions = [];
    d._mock = { truth_total_cents: null, truth_vendor: null, medium: "upload", failing: { R9: "review" }, illegible: [] };
    let url: string | null = null;
    try {
      url = URL.createObjectURL(file);
      this.blobUrls.set(d.id, url);
    } catch {
      url = null;
    }
    d.original_url = url;
    d.pages = url && !isPdfName(file.name) ? [{ index: 0, width: 1237, height: 1600, image_url: url }] : [];
    if (url && !isPdfName(file.name)) d.thumbnail_url = url;
    this.state.docs[d.id] = d;
    this.state.order.push(d.id);
    appendAudit(this.state, d.id, uploaderId ? `user:${uploaderId}` : "visitor", "document.created", null, { filename: d.filename });
    this.emit("document.created", d, batchId);
    const step = this.stepMs;
    setTimeout(() => this.setStatus(d, "Screening"), step);
    setTimeout(() => this.setStatus(d, "Extracting"), step * 2);
    setTimeout(() => {
      // Offline mode cannot read an arbitrary file: say so instead of inventing values.
      d.headline = "Offline preview: connect the InvoiceDesk server to read this invoice";
      d.badges = ["low_confidence"];
      d.rule_results = [
        {
          rule_id: "R9",
          name: "confidence",
          passed: false,
          severity: "review",
          message: "This offline preview does not read files; nothing was extracted, so a person must check it.",
          fields: ["total"],
          details: {},
        },
      ];
      this.setStatus(d, "Extracted");
      this.setStatus(d, "Validating");
      this.setStatus(d, "NeedsReview");
      this.save();
    }, step * 3);
    return d;
  }

  // ------------------------------------------------------------------ reads

  async listDocuments(params: ListParams = {}): Promise<DocumentList> {
    await this.delay();
    const items = this.state.order
      .map((id) => this.state.docs[id])
      .filter((d): d is MockDoc => !!d && !d.session_id)
      .filter((d) => !params.batch_id || d.batch_id === params.batch_id)
      .filter((d) => !params.column || d.column === params.column)
      .filter((d) => !params.status || d.status === params.status)
      .map(toCard);
    const limit = params.limit ?? 200;
    return { items: items.slice(0, limit), next_cursor: null };
  }

  async getBatch(id: string): Promise<BatchView> {
    const batch = this.state.batches.find((b) => b.id === id);
    if (!batch) throw new ApiError(404, "not_found", "That batch doesn't exist.");
    const docs = (await this.listDocuments({ batch_id: id })).items;
    const counts: Partial<Record<KanbanColumn, number>> = {};
    for (const d of docs) counts[d.column] = (counts[d.column] ?? 0) + 1;
    return { batch: clone(batch), counts, cost_usd: this.batchCost(id), documents: docs };
  }

  latestBatchId(): string | null {
    return this.state.batches.at(-1)?.id ?? null;
  }

  async getDocument(id: string): Promise<DocumentDetail> {
    await this.delay();
    const d = this.doc(id);
    if (d.session_id) throw new ApiError(404, "not_found", "That invoice doesn't exist (or was deleted).");
    // Follow-ups live in their own table server-side; the detail lists this document's drafts.
    return { ...toDetail(d), followups: clone(this.state.followups.filter((f) => f.document_id === id)) };
  }

  async getAudit(id: string): Promise<AuditView> {
    this.doc(id);
    return { entries: clone(this.state.audit[id] ?? []), chain_ok: true };
  }

  // ------------------------------------------------------------------ human actions

  async correct(id: string, corrections: Correction[]): Promise<{ status: DocStatus }> {
    const d = this.doc(id);
    if (d.status !== "NeedsReview")
      throw new ApiError(409, "illegal_transition", "Only invoices waiting for review can be corrected.");
    if (corrections.length === 0) throw new ApiError(422, "invalid", "Nothing to correct.");
    const me = this.me_();
    for (const c of corrections) {
      let field = d.fields.find((f) => f.name === c.field);
      if (!field) {
        field = { name: c.field, value: null, display_value: null, raw_conf: null, calibrated_conf: null, band: "red", page: null, box: null, word_ids: [], reason: null };
        d.fields.push(field);
      }
      const before = field.value;
      if (["total", "subtotal", "tax"].includes(c.field)) {
        if (!/^-?\d+$/.test(c.value)) throw new ApiError(422, "invalid", "Amounts must be whole cents.");
        field.display_value = formatCents(Number(c.value));
        if (c.field === "total") d.total_cents = Number(c.value);
      } else field.display_value = c.value;
      Object.assign(field, { value: c.value, calibrated_conf: 1, raw_conf: 1, band: "green", reason: null } satisfies Partial<Field>);
      if (c.field === "vendor_name") d.vendor_name = c.value;
      if (c.field === "invoice_number") d.invoice_number = c.value;
      appendAudit(this.state, d.id, `user:${me.id}`, "review.field_corrected", { field: c.field, value: before }, { field: c.field, value: c.value });
    }
    this.setStatus(d, "Validating", `user:${me.id}`);
    const corrected = new Set(corrections.map((c) => c.field));
    for (const r of d.rule_results) {
      if (r.passed) continue;
      const fixesConfidence = r.rule_id === "R9" && r.fields.some((f) => corrected.has(f)) && d._mock.medium !== "upload";
      const tplTotal = d._mock.truth_total_cents;
      const fixesEstimate = r.rule_id === "R3" && corrected.has("total") && tplTotal !== null && (d.total_cents ?? Infinity) <= tplTotal - 34000;
      if (fixesConfidence || fixesEstimate) {
        Object.assign(r, { passed: true, severity: "pass", message: "Confirmed by a person." });
      }
      appendAudit(this.state, d.id, "system", `rule.${r.rule_id}`, null, { severity: r.severity });
    }
    if (d._mock.medium === "upload" && corrected.size > 0) {
      const r9 = d.rule_results.find((r) => r.rule_id === "R9");
      if (r9) Object.assign(r9, { message: "Still needs the server to check vendor, work order and duplicates." });
    }
    this.reroute(d);
    return { status: d.status };
  }

  private reroute(d: MockDoc): void {
    const failing = failingRules(d);
    const flag = failing.find((r) => r.severity === "flag");
    if (flag) {
      d.headline = flag.message;
      this.setStatus(d, "Flagged");
    } else if (failing.length > 0) {
      d.headline = failing[0]?.message ?? d.headline;
      this.setStatus(d, "NeedsReview");
    } else {
      d.headline = null;
      d.badges = d.badges.filter((b) => b === "handwriting");
      this.setStatus(d, "ReadyToPay");
    }
    this.save();
  }

  async approve(id: string, req: ApprovalRequest): Promise<ApprovalResponse> {
    const d = this.doc(id);
    const me = this.me_();
    if (me.role === "clerk") throw new ApiError(403, "forbidden", "Only approvers and owners can approve.");
    if (d.uploaded_by && d.uploaded_by === me.id)
      throw new ApiError(403, "self_approval", "You uploaded this invoice, so someone else has to approve it.");
    if (d.status !== "Flagged" && d.status !== "NeedsReview")
      throw new ApiError(409, "illegal_transition", "This invoice isn't waiting for an approval.");
    if (req.decision === "approve" && d.approvals.some((a) => a.approver_id === me.id && a.decision === "approve"))
      throw new ApiError(409, "duplicate_approval", "You already approved this one. A second person must approve it.");
    const callbackNeeded = d.approvals_required?.callback ?? req.kind === "bank_change";
    if (req.decision === "approve" && callbackNeeded && !req.callback_attested)
      throw new ApiError(422, "callback_required", "Call the vendor on the phone number on file first, then tick the box.");
    const approval: Approval = {
      kind: req.kind,
      approver_id: me.id,
      decision: req.decision,
      callback_attested: req.callback_attested,
      created_at: nowIso(),
      approver_name: me.display_name,
    };
    d.approvals.push(approval);
    appendAudit(this.state, d.id, `user:${me.id}`, `approval.${req.decision}`, null, {
      kind: req.kind,
      callback_attested: req.callback_attested,
      note: req.note ?? null,
    });
    if (req.decision === "approve") {
      if (d.status === "Flagged") {
        const distinct = new Set(d.approvals.filter((a) => a.decision === "approve").map((a) => a.approver_id));
        const callbacks = d.approvals.some((a) => a.decision === "approve" && a.callback_attested);
        // Mirrors invoicedesk.status._check_approvals: any reject vote blocks release.
        const vetoed = d.approvals.some((a) => a.decision === "reject");
        if (!vetoed && distinct.size >= (d.approvals_required?.count ?? 2) && callbacks) {
          d.headline = "Released by two approvers after a callback to the vendor";
          this.setStatus(d, "ReadyToPay", `user:${me.id}`);
        } else {
          this.emit("document.updated", d, d.batch_id);
        }
      } else {
        const clears: Record<string, string[]> = { new_vendor: ["R1", "R2"], over_cap: ["R7"] };
        for (const r of d.rule_results)
          if (!r.passed && clears[req.kind]?.includes(r.rule_id))
            Object.assign(r, { passed: true, severity: "pass", message: `Approved by ${me.display_name}.` });
        this.setStatus(d, "Validating", `user:${me.id}`);
        this.reroute(d);
      }
    } else {
      this.emit("document.updated", d, d.batch_id);
    }
    this.save();
    return { approvals: clone(d.approvals), status: d.status };
  }

  async reject(id: string, reason: string): Promise<{ status: DocStatus }> {
    const d = this.doc(id);
    if (d.status !== "NeedsReview" && d.status !== "Flagged")
      throw new ApiError(409, "illegal_transition", "Only invoices on hold can be rejected.");
    const me = this.me_();
    appendAudit(this.state, d.id, `user:${me.id}`, "document.rejected", null, { reason });
    d.headline = reason ? `Rejected: ${reason}` : "Rejected";
    this.setStatus(d, "Rejected", `user:${me.id}`);
    this.save();
    return { status: d.status };
  }

  async post(id: string): Promise<{ job_id: string }> {
    const d = this.doc(id);
    if (d.session_id) throw new ApiError(403, "forbidden", "Try-it uploads are never posted to accounting.");
    if (d.status !== "ReadyToPay")
      throw new ApiError(409, "illegal_transition", "Only invoices that are ready to pay can be posted.");
    // A bill needs a vendor and a total; a document released without a reading (for example a
    // quarantined one) cannot be posted until it has been read.
    if (d.total_cents === null || !d.vendor_name)
      throw new ApiError(409, "illegal_transition", "This invoice has no vendor or total on file yet, so it can't be posted.");
    const jobId = uuid();
    const key = `${d.id}:bill`;
    setTimeout(() => {
      if (d.status !== "ReadyToPay") return;
      d.bill = { system: "qbo", external_id: `sandbox-${d.id.slice(0, 6)}`, status: "posted", posted_at: nowIso() };
      appendAudit(this.state, d.id, "system", "bill.posted", null, { idempotency_key: key, external_id: d.bill.external_id });
      this.setStatus(d, "Posted");
      this.save();
    }, this.stepMs * 2);
    return { job_id: jobId };
  }

  async postReady(batchId: string): Promise<{ job_ids: string[]; count: number }> {
    const ready = this.state.order
      .map((id) => this.state.docs[id])
      .filter((d): d is MockDoc => !!d && d.batch_id === batchId && d.status === "ReadyToPay" && !d.session_id && d.total_cents !== null && !!d.vendor_name);
    const job_ids: string[] = [];
    for (const d of ready) {
      job_ids.push((await this.post(d.id)).job_id);
    }
    return { job_ids, count: job_ids.length };
  }

  async createFollowup(documentId: string): Promise<Followup> {
    const d = this.doc(documentId);
    const existing = this.state.followups.find((f) => f.document_id === d.id && !f.sent_at);
    if (existing) return clone(existing);
    const vendor = d.vendor_name ?? "there";
    const issues = failingRules(d).map((r) => `- ${r.message}`);
    const f: Followup = {
      id: uuid(),
      document_id: d.id,
      to_addr: d._mock.vendor_email ?? null,
      draft_subject: `Invoice ${d.invoice_number ?? d.filename}: a quick question`,
      draft_body: `Hi ${vendor} team,\n\nThanks for invoice ${d.invoice_number ?? ""}. Before we can pay it we need a hand with:\n${issues.join("\n") || "- a clearer copy of the invoice"}\n\nCould you reply with a corrected invoice?\n\nThanks,\nMaple Row Property Management`,
      approved_by: null,
      sent_at: null,
    };
    this.state.followups.push(f);
    appendAudit(this.state, d.id, "system", "followup.drafted", null, { followup_id: f.id });
    this.emit("followup.drafted", d, d.batch_id, "Follow-up email drafted");
    this.save();
    return clone(f);
  }

  async listFollowups(status?: "draft" | "approved" | "sent"): Promise<{ items: Followup[] }> {
    const items = this.state.followups.filter((f) => {
      if (!status) return true;
      if (status === "sent") return !!f.sent_at;
      if (status === "approved") return !!f.approved_by;
      return !f.approved_by;
    });
    return { items: clone(items) };
  }

  async approveFollowup(id: string, edits: { subject?: string; body?: string } = {}): Promise<Followup> {
    const f = this.state.followups.find((x) => x.id === id);
    if (!f) throw new ApiError(404, "not_found", "That draft doesn't exist.");
    if (f.approved_by) throw new ApiError(409, "already_approved", "This email was already approved.");
    const me = this.me_();
    if (edits.subject !== undefined) f.draft_subject = edits.subject;
    if (edits.body !== undefined) f.draft_body = edits.body;
    f.approved_by = me.id;
    f.sent_at = nowIso(); // mock: the n8n -> Mailpit send is simulated as immediate
    appendAudit(this.state, f.document_id, `user:${me.id}`, "followup.approved", null, { followup_id: f.id });
    this.save();
    return clone(f);
  }

  // ------------------------------------------------------------------ dashboards

  async dashboard(_days = 30): Promise<Dashboard> {
    return clone(FIX.dashboard);
  }

  async comparison(batchId: string): Promise<Comparison> {
    const docs = this.state.order.map((id) => this.state.docs[id]).filter((d): d is MockDoc => !!d && d.batch_id === batchId);
    const byName = new Map(docs.map((d) => [d.filename, d.id]));
    const rows = FIX.comparison.rows
      .filter((r) => byName.has(r.filename))
      .map((r) => ({ ...clone(r), document_id: byName.get(r.filename) as string }));
    if (rows.length === 0) throw new ApiError(404, "not_found", "No comparison is available for this batch yet.");
    const wrong = rows.filter((r) => r.naive.verdict === "wrong");
    return {
      rows,
      summary: {
        naive_wrong: wrong.length,
        invoicedesk_wrong: rows.filter((r) => r.invoicedesk.verdict === "wrong").length,
        naive_dollars_lost_cents: FIX.comparison.summary.naive_dollars_lost_cents,
      },
    };
  }

  async scorecard(): Promise<Scorecard | null> {
    return clone(FIX.scorecard);
  }

  reportCardUrl(): string {
    return "/mock/report-card.pdf";
  }

  // ------------------------------------------------------------------ try-it

  async tryitSession(turnstileToken: string): Promise<TryitSession> {
    if (!turnstileToken) throw new ApiError(403, "turnstile_failed", "Please complete the human check first.");
    this.state.tryit.session_id ??= uuid();
    this.save();
    return { session_id: this.state.tryit.session_id, uploads_remaining: this.state.tryit.uploads_remaining };
  }

  async tryitStatus(): Promise<TryitStatus> {
    const capped = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("spendcap") === "1";
    return {
      spend_cap_reached: capped,
      uploads_remaining: this.state.tryit.uploads_remaining,
      replay_batch_id: capped ? (this.state.batches[0]?.id ?? null) : null,
    };
  }

  async tryitUpload(file: File): Promise<TryitUploadResponse> {
    const sid = this.state.tryit.session_id;
    if (!sid) throw new ApiError(403, "turnstile_failed", "Please complete the human check first.");
    const status = await this.tryitStatus();
    if (status.spend_cap_reached && status.replay_batch_id) return { replay: true, replay_batch_id: status.replay_batch_id };
    if (this.state.tryit.uploads_remaining <= 0)
      throw new ApiError(429, "rate_limited", "You've used all 3 uploads for this session. Try again tomorrow.");
    const { rejected } = splitUploads([file]);
    if (rejected[0]) throw new ApiError(422, "rejected_input", rejected[0].error);
    this.state.tryit.uploads_remaining -= 1;
    const d = this.uploadedDoc(file, null, null, sid);
    this.save();
    return { document_id: d.id };
  }

  async tryitDocument(id: string): Promise<DocumentDetail> {
    const d = this.doc(id);
    if (d.session_id !== this.state.tryit.session_id) throw new ApiError(404, "not_found", "That upload isn't in your session.");
    const detail = toDetail(d);
    // Visitors never see remit-to bank details, the original file link, traces or approvals.
    return {
      ...detail,
      fields: detail.fields.filter((f) => f.name !== "remit_to_bank_details"),
      original_url: null,
      trace_url: null,
      approvals: [],
      approvals_required: null,
    };
  }

  // ------------------------------------------------------------------ realtime

  subscribe(batchId: string | null, onEvent: (e: BoardEvent) => void, onState?: (s: ConnectionState) => void): Subscription {
    const l = { batchId, fn: onEvent };
    this.listeners.add(l);
    onState?.("live");
    return { close: () => void this.listeners.delete(l) };
  }
}
