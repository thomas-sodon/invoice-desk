/**
 * Types for the InvoiceDesk REST + realtime contract (docs/api-contract.md).
 *
 * These mirror the JSON exactly: money is integer cents in `*_cents` fields, API cost is a USD
 * decimal string, dates are ISO strings. Runtime guards at the bottom validate untrusted JSON
 * (SSE payloads, API responses) before the UI uses it.
 */

export const DOC_STATUSES = [
  "Received",
  "Screening",
  "Quarantined",
  "Extracting",
  "Extracted",
  "Validating",
  "ReadyToPay",
  "NeedsReview",
  "Flagged",
  "Rejected",
  "Posted",
] as const;
export type DocStatus = (typeof DOC_STATUSES)[number];

export const KANBAN_COLUMNS = [
  "received",
  "extracted",
  "validated",
  "ready_to_pay",
  "needs_review",
  "flagged",
  "posted",
  "rejected",
] as const;
export type KanbanColumn = (typeof KANBAN_COLUMNS)[number];

export const BADGES = [
  "duplicate",
  "bank_change",
  "hidden_text",
  "faint_text",
  "injection",
  "over_cap",
  "new_vendor",
  "lookalike",
  "low_confidence",
  "handwriting",
  "over_estimate",
  "arithmetic",
  "date",
] as const;
export type Badge = (typeof BADGES)[number];

export type Band = "green" | "amber" | "red";
export type Severity = "pass" | "review" | "flag";
export type Source = "upload" | "email" | "seed" | "mcp";
/** USD amount as a decimal string, e.g. "0.0112". Never a float. */
export type UsdString = string;
/** Integer number of cents. */
export type Cents = number;
export type Box = [number, number, number, number];

export interface DocumentCard {
  id: string;
  batch_id: string | null;
  filename: string;
  source: Source;
  status: DocStatus;
  column: KanbanColumn;
  vendor_name: string | null;
  invoice_number: string | null;
  total_cents: Cents | null;
  headline: string | null;
  /** Known values are in BADGES; unknown ones (e.g. "work_order") still render. */
  badges: string[];
  cost_usd: UsdString;
  thumbnail_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface Field {
  name: string;
  value: string | null;
  display_value: string | null;
  raw_conf: number | null;
  calibrated_conf: number | null;
  band: Band;
  page: number | null;
  box: Box | null;
  word_ids: number[];
  reason: string | null;
}

export interface RuleResult {
  rule_id: string;
  name: string;
  passed: boolean;
  severity: Severity;
  message: string;
  fields: string[];
  details: Record<string, unknown>;
}

export type Detector = "hidden_text" | "faint_text" | "injection_classifier";
export type Verdict = "clean" | "suspicious" | "malicious";

export interface ScreenSpan {
  text: string;
  reason: string;
  page: number | null;
  box: Box | null;
  source: string;
}

export interface ScreenResult {
  detector: Detector;
  verdict: Verdict;
  categories: string[];
  spans: ScreenSpan[];
}

export interface PageImage {
  index: number;
  width: number;
  height: number;
  image_url: string;
}

export interface LineItem {
  description: string | null;
  quantity: string | null;
  unit_price_cents: Cents | null;
  amount_cents: Cents | null;
  box: Box | null;
  page: number | null;
}

export interface ExtractionRun {
  tier: number;
  model_id: string;
  cost_usd: UsdString;
  latency_ms: number;
  refused: boolean;
  cache_read_tokens: number;
}

export type ApprovalKind = "bank_change" | "over_cap" | "new_vendor" | "duplicate" | "injection" | "lookalike" | string;
export type Decision = "approve" | "reject";

export interface Approval {
  kind: ApprovalKind;
  approver_id: string;
  decision: Decision;
  callback_attested: boolean;
  created_at: string;
  /** Not in the contract yet; shown when the API provides it. */
  approver_name?: string | null;
}

export interface ApprovalsRequired {
  count: number;
  callback: boolean;
}

export interface BillInfo {
  system: string;
  external_id: string | null;
  status: string;
  posted_at: string | null;
}

export interface Followup {
  id: string;
  document_id: string;
  to_addr: string | null;
  draft_subject: string;
  draft_body: string;
  approved_by: string | null;
  sent_at: string | null;
}

/** A region of the page image that must be covered (bank or account numbers printed on it). */
export interface Redaction {
  page: number;
  box: Box;
  /** Masked replacement shown on the bar, e.g. "Acct ••••7128". */
  label: string | null;
}

export interface DocumentDetail extends DocumentCard {
  pages: PageImage[];
  original_url: string | null;
  fields: Field[];
  line_items: LineItem[];
  rule_results: RuleResult[];
  screen_results: ScreenResult[];
  embedded_instructions: string[];
  extractions: ExtractionRun[];
  approvals: Approval[];
  approvals_required: ApprovalsRequired | null;
  bill: BillInfo | null;
  followups: Followup[];
  email: { from: string; subject: string } | null;
  trace_url: string | null;
  /**
   * Staff id of the uploader. Requested as a contract addition so the UI can hide the approve
   * buttons from the uploader; the API enforces the rule regardless (403 self_approval).
   */
  uploaded_by?: string | null;
  /**
   * Requested contract addition: page regions holding bank/account numbers. The UI paints an
   * opaque bar over each so the original image never shows a full number (CLAUDE.md invariant 9).
   */
  redactions?: Redaction[];
}

export interface DocumentList {
  items: DocumentCard[];
  next_cursor: string | null;
}

export interface BatchInfo {
  id: string;
  label: string;
  mode: string;
  created_at: string;
}

export interface BatchView {
  batch: BatchInfo;
  counts: Partial<Record<KanbanColumn, number>>;
  cost_usd: UsdString;
  documents: DocumentCard[];
}

export interface UploadResponse {
  batch_id: string;
  documents: { id: string; filename: string; status: DocStatus }[];
  rejected: { filename: string; error: string }[];
}

export interface DemoRunResponse {
  batch_id: string;
  document_ids: string[];
}

export interface AuditEntry {
  id: number | string;
  ts: string;
  actor: string;
  action: string;
  before: unknown;
  after: unknown;
  row_hash: string;
}

export interface AuditView {
  entries: AuditEntry[];
  chain_ok: boolean;
}

export interface Correction {
  field: string;
  value: string;
}

export interface ApprovalRequest {
  kind: ApprovalKind;
  decision: Decision;
  callback_attested: boolean;
  note?: string;
}

export interface ApprovalResponse {
  approvals: Approval[];
  status: DocStatus;
}

export interface Dashboard {
  invoices: number;
  hands_free_pct: number;
  hours_saved: number;
  dollars_caught_cents: { duplicates: Cents; fraud: Cents; overcharges: Cents; total: Cents };
  cost_per_invoice_usd: UsdString;
  manual_benchmark_per_invoice_cents: Cents;
  assumptions: { minutes_per_invoice: number } & Record<string, number>;
  label: string;
}

export type Verdict2 = "wrong" | "correct";

export interface ComparisonSide {
  outcome: string;
  verdict: Verdict2;
  reason: string;
  amount_cents?: Cents | null;
}

export interface ComparisonRow {
  document_id: string;
  filename: string;
  naive: ComparisonSide;
  invoicedesk: ComparisonSide;
}

export interface Comparison {
  rows: ComparisonRow[];
  summary: { naive_wrong: number; invoicedesk_wrong: number; naive_dollars_lost_cents: Cents };
}

export interface Metric {
  value: number | string;
  ci?: [number, number] | null;
}

export interface ReliabilityBin {
  /** Mean predicted confidence in the bin (0..1). */
  confidence: number;
  /** Observed accuracy in the bin (0..1). */
  accuracy: number;
  count: number;
}

export interface Scorecard {
  run_id: string;
  git_sha: string;
  config: string;
  finished_at: string;
  metrics: Record<string, Metric | undefined>;
  label: string;
  /** Optional: reliability diagram bins when the eval run stored them. */
  reliability?: ReliabilityBin[] | null;
}

export interface TryitSession {
  session_id: string;
  uploads_remaining: number;
}

export interface TryitStatus {
  spend_cap_reached: boolean;
  uploads_remaining: number;
  replay_batch_id: string | null;
}

export type TryitUploadResponse = { document_id: string } | { replay: true; replay_batch_id: string };

export interface Me {
  id: string;
  email: string;
  display_name: string;
  role: "clerk" | "approver" | "owner";
}

export const EVENT_TYPES = [
  "document.created",
  "document.status_changed",
  "document.updated",
  "batch.cost_updated",
  "batch.completed",
  "followup.drafted",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export interface BoardEvent {
  type: EventType;
  tenant_id: string;
  document_id: string | null;
  batch_id: string | null;
  status: DocStatus | null;
  column: KanbanColumn | null;
  cost_usd: UsdString | null;
  batch_cost_usd: UsdString | null;
  message: string | null;
  ts: string;
}

export interface ApiErrorBody {
  error: { code: string; message: string };
}

// ---------------------------------------------------------------------------------------------
// Runtime guards
// ---------------------------------------------------------------------------------------------

type Obj = Record<string, unknown>;

export function isObject(v: unknown): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isStr(v: unknown): v is string {
  return typeof v === "string";
}

function isNullableStr(v: unknown): v is string | null {
  return v === null || typeof v === "string";
}

export function isDocStatus(v: unknown): v is DocStatus {
  return isStr(v) && (DOC_STATUSES as readonly string[]).includes(v);
}

export function isKanbanColumn(v: unknown): v is KanbanColumn {
  return isStr(v) && (KANBAN_COLUMNS as readonly string[]).includes(v);
}

export function isBadge(v: unknown): v is Badge {
  return isStr(v) && (BADGES as readonly string[]).includes(v);
}

/** Integer cents: a safe integer (negative allowed for credits). Floats are rejected. */
export function isCents(v: unknown): v is Cents {
  return typeof v === "number" && Number.isSafeInteger(v);
}

/** A USD decimal string such as "0.0112" or "12". Numbers (floats) are rejected on purpose. */
export function isUsdString(v: unknown): v is UsdString {
  return isStr(v) && /^-?\d+(\.\d+)?$/.test(v);
}

function isNullableUsd(v: unknown): v is UsdString | null {
  return v === null || isUsdString(v);
}

export function isBox(v: unknown): v is Box {
  return Array.isArray(v) && v.length === 4 && v.every((n) => typeof n === "number" && Number.isFinite(n));
}

export function isBand(v: unknown): v is Band {
  return v === "green" || v === "amber" || v === "red";
}

export function isEventType(v: unknown): v is EventType {
  return isStr(v) && (EVENT_TYPES as readonly string[]).includes(v);
}

export function isBoardEvent(v: unknown): v is BoardEvent {
  if (!isObject(v)) return false;
  return (
    isEventType(v.type) &&
    isStr(v.tenant_id) &&
    isNullableStr(v.document_id ?? null) &&
    isNullableStr(v.batch_id ?? null) &&
    (v.status == null || isDocStatus(v.status)) &&
    (v.column == null || isKanbanColumn(v.column)) &&
    isNullableUsd(v.cost_usd ?? null) &&
    isNullableUsd(v.batch_cost_usd ?? null) &&
    isNullableStr(v.message ?? null) &&
    isStr(v.ts)
  );
}

/** Normalizes optional/omitted keys to null after a successful guard. */
export function normalizeBoardEvent(v: BoardEvent): BoardEvent {
  return {
    type: v.type,
    tenant_id: v.tenant_id,
    document_id: v.document_id ?? null,
    batch_id: v.batch_id ?? null,
    status: v.status ?? null,
    column: v.column ?? null,
    cost_usd: v.cost_usd ?? null,
    batch_cost_usd: v.batch_cost_usd ?? null,
    message: v.message ?? null,
    ts: v.ts,
  };
}

export function isDocumentCard(v: unknown): v is DocumentCard {
  if (!isObject(v)) return false;
  return (
    isStr(v.id) &&
    isNullableStr(v.batch_id) &&
    isStr(v.filename) &&
    isDocStatus(v.status) &&
    isKanbanColumn(v.column) &&
    isNullableStr(v.vendor_name) &&
    isNullableStr(v.invoice_number) &&
    (v.total_cents === null || isCents(v.total_cents)) &&
    Array.isArray(v.badges) &&
    isUsdString(v.cost_usd)
  );
}

export function isField(v: unknown): v is Field {
  if (!isObject(v)) return false;
  return (
    isStr(v.name) &&
    isNullableStr(v.value) &&
    isBand(v.band) &&
    (v.box === null || v.box === undefined || isBox(v.box)) &&
    (v.calibrated_conf === null || typeof v.calibrated_conf === "number")
  );
}

export function isRuleResult(v: unknown): v is RuleResult {
  if (!isObject(v)) return false;
  return (
    isStr(v.rule_id) &&
    isStr(v.name) &&
    typeof v.passed === "boolean" &&
    (v.severity === "pass" || v.severity === "review" || v.severity === "flag") &&
    isStr(v.message)
  );
}

export function isDocumentDetail(v: unknown): v is DocumentDetail {
  if (!isDocumentCard(v)) return false;
  const o = v as unknown as Obj;
  return (
    Array.isArray(o.pages) &&
    Array.isArray(o.fields) &&
    o.fields.every(isField) &&
    Array.isArray(o.rule_results) &&
    o.rule_results.every(isRuleResult) &&
    Array.isArray(o.screen_results) &&
    Array.isArray(o.extractions) &&
    Array.isArray(o.approvals)
  );
}

export function isApiErrorBody(v: unknown): v is ApiErrorBody {
  return isObject(v) && isObject(v.error) && isStr(v.error.code) && isStr(v.error.message);
}

export function isReplayUpload(v: TryitUploadResponse): v is { replay: true; replay_batch_id: string } {
  return "replay" in v && v.replay === true;
}
