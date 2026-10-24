/**
 * Typed API client for docs/api-contract.md. `getApi()` returns the mock client (fixture data,
 * no backend) when NEXT_PUBLIC_API_MOCK=1 or no NEXT_PUBLIC_API_BASE is set, otherwise the HTTP
 * client against `${NEXT_PUBLIC_API_BASE}/api/v1` with SSE for realtime.
 */
import type {
  ApprovalRequest,
  ApprovalResponse,
  AuditView,
  BatchView,
  BoardEvent,
  Comparison,
  Correction,
  Dashboard,
  DemoRunResponse,
  DocStatus,
  DocumentDetail,
  DocumentList,
  Followup,
  KanbanColumn,
  Me,
  Scorecard,
  TryitSession,
  TryitStatus,
  TryitUploadResponse,
  UploadResponse,
} from "./api-types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export interface ListParams {
  batch_id?: string;
  column?: KanbanColumn;
  status?: DocStatus;
  limit?: number;
  cursor?: string;
}

export type ConnectionState = "connecting" | "live" | "reconnecting";

export interface Subscription {
  close(): void;
}

export interface ApiClient {
  readonly mode: "mock" | "http";
  /** Mock only: the most recent batch, so the board opens on it. */
  latestBatchId?(): string | null;

  me(): Promise<Me | null>;

  createBatch(label: string): Promise<{ batch_id: string }>;
  uploadDocuments(files: File[], batchId?: string): Promise<UploadResponse>;
  runDemo(mode: "live" | "replay"): Promise<DemoRunResponse>;

  listDocuments(params?: ListParams): Promise<DocumentList>;
  getBatch(id: string): Promise<BatchView>;
  getDocument(id: string): Promise<DocumentDetail>;
  getAudit(id: string): Promise<AuditView>;

  correct(id: string, corrections: Correction[]): Promise<{ status: DocStatus }>;
  approve(id: string, req: ApprovalRequest): Promise<ApprovalResponse>;
  reject(id: string, reason: string): Promise<{ status: DocStatus }>;
  post(id: string): Promise<{ job_id: string }>;
  postReady(batchId: string): Promise<{ job_ids: string[]; count: number }>;

  createFollowup(documentId: string): Promise<Followup>;
  listFollowups(status?: "draft" | "approved" | "sent"): Promise<{ items: Followup[] }>;
  approveFollowup(id: string, edits?: { subject?: string; body?: string }): Promise<Followup>;

  dashboard(days?: number): Promise<Dashboard>;
  comparison(batchId: string): Promise<Comparison>;
  /** Resolves null when no eval run exists yet (404). */
  scorecard(): Promise<Scorecard | null>;
  reportCardUrl(): string;

  tryitSession(turnstileToken: string): Promise<TryitSession>;
  tryitStatus(): Promise<TryitStatus>;
  tryitUpload(file: File): Promise<TryitUploadResponse>;
  tryitDocument(id: string): Promise<DocumentDetail>;

  /** Realtime board events (SSE in HTTP mode, a local emitter in mock mode). */
  subscribe(
    batchId: string | null,
    onEvent: (e: BoardEvent) => void,
    onState?: (s: ConnectionState) => void,
  ): Subscription;
}

export function isMockMode(env: Record<string, string | undefined> = readPublicEnv()): boolean {
  if (env.NEXT_PUBLIC_API_MOCK === "1") return true;
  if (env.NEXT_PUBLIC_API_MOCK === "0") return false;
  return !env.NEXT_PUBLIC_API_BASE;
}

/** Next inlines NEXT_PUBLIC_* only for literal property access, so read them explicitly. */
export function readPublicEnv(): Record<string, string | undefined> {
  return {
    NEXT_PUBLIC_API_MOCK: process.env.NEXT_PUBLIC_API_MOCK,
    NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE,
    NEXT_PUBLIC_DEV_USER: process.env.NEXT_PUBLIC_DEV_USER,
    NEXT_PUBLIC_TURNSTILE_SITE_KEY: process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY,
  };
}

let client: ApiClient | null = null;

export async function loadApi(): Promise<ApiClient> {
  if (client) return client;
  const env = readPublicEnv();
  if (isMockMode(env)) {
    const { MockApiClient } = await import("./mock/server");
    client = new MockApiClient();
  } else {
    const { HttpApiClient } = await import("./http-client");
    client = new HttpApiClient(env.NEXT_PUBLIC_API_BASE ?? "");
  }
  return client;
}

/** Test hook: replace or reset the singleton. */
export function setApiForTests(c: ApiClient | null): void {
  client = c;
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return "Something went wrong.";
}
