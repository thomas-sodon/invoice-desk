import { ApiError, type ApiClient, type ListParams, type Subscription, type ConnectionState } from "./api";
import {
  isApiErrorBody,
  isDocumentDetail,
  type ApprovalRequest,
  type ApprovalResponse,
  type AuditView,
  type BatchView,
  type BoardEvent,
  type Comparison,
  type Correction,
  type Dashboard,
  type DemoRunResponse,
  type DocStatus,
  type DocumentDetail,
  type DocumentList,
  type Followup,
  type Me,
  type Scorecard,
  type TryitSession,
  type TryitStatus,
  type TryitUploadResponse,
  type UploadResponse,
} from "./api-types";
import { authHeaders } from "./auth";
import { openEventStream } from "./sse";

type Json = Record<string, unknown> | unknown[] | null;

export class HttpApiClient implements ApiClient {
  readonly mode = "http" as const;
  private readonly root: string;
  private readonly fetchImpl: typeof fetch;

  constructor(base: string, fetchImpl?: typeof fetch) {
    this.root = `${base.replace(/\/+$/, "")}/api/v1`;
    this.fetchImpl = fetchImpl ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
  }

  url(path: string, query?: Record<string, string | number | undefined | null>): string {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
    const qs = q.toString();
    return `${this.root}${path}${qs ? `?${qs}` : ""}`;
  }

  private async request<T>(
    method: string,
    path: string,
    opts: { body?: Json | FormData; query?: Record<string, string | number | undefined | null>; allow404?: boolean } = {},
  ): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json", ...authHeaders() };
    let body: BodyInit | undefined;
    if (opts.body instanceof FormData) body = opts.body;
    else if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }
    let res: Response;
    try {
      res = await this.fetchImpl(this.url(path, opts.query), { method, headers, body, credentials: "include" });
    } catch {
      throw new ApiError(0, "network", "Can't reach the InvoiceDesk server. Check your connection and try again.");
    }
    if (opts.allow404 && res.status === 404) return null as T;
    const text = await res.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = null;
      }
    }
    if (!res.ok) {
      if (isApiErrorBody(parsed)) throw new ApiError(res.status, parsed.error.code, parsed.error.message);
      throw new ApiError(res.status, "http_error", `Request failed (${res.status}).`);
    }
    return parsed as T;
  }

  me(): Promise<Me | null> {
    // GET /me is a requested contract addition; a 404 means "identity unknown" and the API still
    // enforces approver rules server-side.
    return this.request<Me | null>("GET", "/me", { allow404: true }).catch(() => null);
  }

  createBatch(label: string) {
    return this.request<{ batch_id: string }>("POST", "/batches", { body: { label, mode: "live" } });
  }

  uploadDocuments(files: File[], batchId?: string) {
    const fd = new FormData();
    for (const f of files) fd.append("files[]", f, f.name);
    if (batchId) fd.append("batch_id", batchId);
    return this.request<UploadResponse>("POST", "/documents", { body: fd });
  }

  runDemo(mode: "live" | "replay") {
    return this.request<DemoRunResponse>("POST", "/demo/run", { body: { mode } });
  }

  listDocuments(params: ListParams = {}) {
    return this.request<DocumentList>("GET", "/documents", { query: { ...params } });
  }

  getBatch(id: string) {
    return this.request<BatchView>("GET", `/batches/${encodeURIComponent(id)}`);
  }

  async getDocument(id: string): Promise<DocumentDetail> {
    const d = await this.request<unknown>("GET", `/documents/${encodeURIComponent(id)}`);
    if (!isDocumentDetail(d)) throw new ApiError(502, "bad_response", "The server sent an invoice the app could not read.");
    return d;
  }

  getAudit(id: string) {
    return this.request<AuditView>("GET", `/documents/${encodeURIComponent(id)}/audit`);
  }

  correct(id: string, corrections: Correction[]) {
    return this.request<{ status: DocStatus }>("POST", `/documents/${encodeURIComponent(id)}/corrections`, {
      body: { corrections },
    });
  }

  approve(id: string, req: ApprovalRequest) {
    return this.request<ApprovalResponse>("POST", `/documents/${encodeURIComponent(id)}/approvals`, {
      body: { ...req },
    });
  }

  reject(id: string, reason: string) {
    return this.request<{ status: DocStatus }>("POST", `/documents/${encodeURIComponent(id)}/reject`, { body: { reason } });
  }

  post(id: string) {
    return this.request<{ job_id: string }>("POST", `/documents/${encodeURIComponent(id)}/post`);
  }

  postReady(batchId: string) {
    return this.request<{ job_ids: string[]; count: number }>("POST", `/batches/${encodeURIComponent(batchId)}/post-ready`);
  }

  createFollowup(documentId: string) {
    return this.request<Followup>("POST", `/documents/${encodeURIComponent(documentId)}/followups`);
  }

  listFollowups(status?: "draft" | "approved" | "sent") {
    return this.request<{ items: Followup[] }>("GET", "/followups", { query: { status } });
  }

  approveFollowup(id: string, edits: { subject?: string; body?: string } = {}) {
    return this.request<Followup>("POST", `/followups/${encodeURIComponent(id)}/approve`, { body: { ...edits } });
  }

  dashboard(days = 30) {
    return this.request<Dashboard>("GET", "/dashboard", { query: { days } });
  }

  comparison(batchId: string) {
    return this.request<Comparison>("GET", `/batches/${encodeURIComponent(batchId)}/comparison`);
  }

  scorecard() {
    return this.request<Scorecard | null>("GET", "/scorecard/latest", { allow404: true });
  }

  reportCardUrl() {
    return this.url("/report-card.pdf");
  }

  tryitSession(turnstileToken: string) {
    return this.request<TryitSession>("POST", "/tryit/session", { body: { turnstile_token: turnstileToken } });
  }

  tryitStatus() {
    return this.request<TryitStatus>("GET", "/tryit/status");
  }

  tryitUpload(file: File) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return this.request<TryitUploadResponse>("POST", "/tryit/upload", { body: fd });
  }

  async tryitDocument(id: string): Promise<DocumentDetail> {
    const d = await this.request<unknown>("GET", `/tryit/documents/${encodeURIComponent(id)}`);
    if (!isDocumentDetail(d)) throw new ApiError(502, "bad_response", "The server sent an invoice the app could not read.");
    return d;
  }

  subscribe(batchId: string | null, onEvent: (e: BoardEvent) => void, onState?: (s: ConnectionState) => void): Subscription {
    return openEventStream({
      url: this.url("/events", { batch_id: batchId }),
      headers: authHeaders(),
      onEvent,
      onState,
      fetchImpl: this.fetchImpl,
    });
  }
}
