"""Pydantic request/response models mirroring docs/api-contract.md exactly.

Money is integer cents (``*_cents``), API cost is a USD decimal *string*, dates are ISO strings.
Two requested contract additions that the web app already reads are included as optional
fields: ``DocumentDetail.uploaded_by`` and ``DocumentDetail.redactions`` (see apps/web
lib/api-types.ts), plus ``GET /me``.
"""

from __future__ import annotations

from typing import Any, Literal

from invoicedesk.money import Cents
from pydantic import BaseModel, ConfigDict, Field

Box = tuple[int, int, int, int]
Usd = str


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------- shared shapes
class DocumentCard(_Out):
    id: str
    batch_id: str | None
    filename: str
    source: Literal["upload", "email", "seed", "mcp"]
    status: str
    column: str
    vendor_name: str | None
    invoice_number: str | None
    total_cents: int | None
    headline: str | None
    badges: list[str]
    cost_usd: Usd
    thumbnail_url: str | None
    created_at: str
    updated_at: str


class Field_(_Out):
    name: str
    value: str | None
    display_value: str | None
    raw_conf: float | None
    calibrated_conf: float | None
    band: Literal["green", "amber", "red"]
    page: int | None
    box: Box | None
    word_ids: list[int]
    reason: str | None


class RuleResultOut(_Out):
    rule_id: str
    name: str
    passed: bool
    severity: Literal["pass", "review", "flag"]
    message: str
    fields: list[str]
    details: dict[str, Any]


class ScreenSpanOut(_Out):
    text: str
    reason: str
    page: int | None
    box: Box | None
    source: str


class ScreenResultOut(_Out):
    detector: str
    verdict: Literal["clean", "suspicious", "malicious"]
    categories: list[str]
    spans: list[ScreenSpanOut]


class PageOut(_Out):
    index: int
    width: int
    height: int
    image_url: str


class LineItemOut(_Out):
    description: str | None
    quantity: str | None
    unit_price_cents: int | None
    amount_cents: int | None
    box: Box | None
    page: int | None


class ExtractionRunOut(_Out):
    tier: int
    model_id: str
    cost_usd: Usd
    latency_ms: int
    refused: bool
    cache_read_tokens: int


class ApprovalOut(_Out):
    kind: str
    approver_id: str
    decision: Literal["approve", "reject"]
    callback_attested: bool
    created_at: str
    approver_name: str | None = None


class ApprovalsRequired(_Out):
    count: int
    callback: bool


class BillOut(_Out):
    system: str
    external_id: str | None
    status: str
    posted_at: str | None


class Followup(_Out):
    id: str
    document_id: str
    to_addr: str
    draft_subject: str
    draft_body: str
    approved_by: str | None
    sent_at: str | None


class EmailInfo(_Out):
    from_: str = Field(alias="from")
    subject: str

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)


class Redaction(_Out):
    page: int
    box: Box
    label: str | None


class DocumentDetail(DocumentCard):
    pages: list[PageOut]
    original_url: str | None
    fields: list[Field_]
    line_items: list[LineItemOut]
    rule_results: list[RuleResultOut]
    screen_results: list[ScreenResultOut]
    embedded_instructions: list[str]
    extractions: list[ExtractionRunOut]
    approvals: list[ApprovalOut]
    approvals_required: ApprovalsRequired | None
    bill: BillOut | None
    followups: list[Followup]
    email: EmailInfo | None
    trace_url: str | None
    uploaded_by: str | None = None
    redactions: list[Redaction] = Field(default_factory=list)


class DocumentList(_Out):
    items: list[DocumentCard]
    next_cursor: str | None


class BatchInfo(_Out):
    id: str
    label: str
    mode: str
    created_at: str


class BatchView(_Out):
    batch: BatchInfo
    counts: dict[str, int]
    cost_usd: Usd
    documents: list[DocumentCard]


# ---------------------------------------------------------------------------- ingest
class BatchCreate(_In):
    label: str = Field("", max_length=200)
    mode: Literal["live", "replay"] = "live"


class BatchCreated(_Out):
    batch_id: str


class UploadedDoc(_Out):
    id: str
    filename: str
    status: str


class RejectedFile(_Out):
    filename: str
    error: str


class UploadResponse(_Out):
    batch_id: str
    documents: list[UploadedDoc]
    rejected: list[RejectedFile]


class DemoRunRequest(_In):
    mode: Literal["live", "replay"] = "replay"


class DemoRunResponse(_Out):
    batch_id: str
    document_ids: list[str]


class EmailAttachment(_In):
    filename: str = Field(max_length=255)
    content_type: str = Field(max_length=200)
    content_b64: str


class EmailIngest(_In):
    message_id: str = Field(min_length=1, max_length=998)
    from_: str = Field(alias="from", min_length=1, max_length=320)
    subject: str = Field("", max_length=998)
    body_text: str = Field("", max_length=200_000)
    received_at: str | None = None
    attachments: list[EmailAttachment] = Field(default_factory=list, max_length=10)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EmailDoc(_Out):
    id: str
    filename: str


class EmailIngestResponse(_Out):
    thread_id: str
    documents: list[EmailDoc]
    rejected: list[RejectedFile] = Field(default_factory=list)


# ---------------------------------------------------------------------------- audit
class AuditEntry(_Out):
    id: int
    ts: str
    actor: str
    action: str
    before: Any
    after: Any
    row_hash: str


class AuditView(_Out):
    entries: list[AuditEntry]
    chain_ok: bool


# ---------------------------------------------------------------------------- human actions
class CorrectionItem(_In):
    field: str = Field(max_length=64)
    value: str | int | None


class CorrectionsRequest(_In):
    corrections: list[CorrectionItem] = Field(min_length=1, max_length=20)


class StatusOut(_Out):
    status: str


class ApprovalRequest(_In):
    kind: Literal["bank_change", "over_cap", "new_vendor", "duplicate", "injection", "other"]
    decision: Literal["approve", "reject"]
    callback_attested: bool = False
    note: str | None = Field(None, max_length=2000)


class ApprovalResponse(_Out):
    approvals: list[ApprovalOut]
    status: str


class RejectRequest(_In):
    reason: str = Field("", max_length=2000)


class JobOut(_Out):
    job_id: str


class PostReadyOut(_Out):
    job_ids: list[str]
    count: int


class FollowupList(_Out):
    items: list[Followup]


class FollowupApprove(_In):
    subject: str | None = Field(None, max_length=300)
    body: str | None = Field(None, max_length=5000)


# ---------------------------------------------------------------------------- owner
class DollarsCaught(_Out):
    duplicates: int
    fraud: int
    overcharges: int
    total: int


class Dashboard(_Out):
    invoices: int
    hands_free_pct: float
    hours_saved: float
    dollars_caught_cents: DollarsCaught
    cost_per_invoice_usd: Usd
    manual_benchmark_per_invoice_cents: int
    assumptions: dict[str, int]
    label: str


class ComparisonSide(_Out):
    outcome: str
    verdict: str
    reason: str
    amount_cents: int | None = None


class ComparisonRow(_Out):
    document_id: str
    filename: str | None
    naive: ComparisonSide | None
    invoicedesk: ComparisonSide


class ComparisonSummary(_Out):
    naive_wrong: int
    invoicedesk_wrong: int
    naive_dollars_lost_cents: int


class Comparison(_Out):
    rows: list[ComparisonRow]
    summary: ComparisonSummary
    label: str = "demo comparison on synthetic data"


class Scorecard(_Out):
    run_id: str
    git_sha: str
    config: str
    finished_at: str
    metrics: dict[str, Any]
    label: str
    reliability: list[dict[str, Any]] | None = None


class Me(_Out):
    id: str
    email: str
    display_name: str
    role: str


# ---------------------------------------------------------------------------- try-it
class TryitSessionRequest(_In):
    turnstile_token: str = Field(max_length=2048)


class TryitSession(_Out):
    session_id: str
    uploads_remaining: int
    banner: str


class TryitStatus(_Out):
    spend_cap_reached: bool
    uploads_remaining: int
    replay_batch_id: str | None
    banner: str


class TryitUploaded(_Out):
    document_id: str


class TryitReplay(_Out):
    replay: Literal[True]
    replay_batch_id: str


# ---------------------------------------------------------------------------- agent facade
class AgentExtractRequest(_In):
    file_url: str | None = Field(None, max_length=2048)
    upload_handle: str | None = Field(None, max_length=512)


class AgentField(_Out):
    value: str | None
    calibrated_confidence: float | None


class AgentExtractResponse(_Out):
    document_id: str
    status: str
    fields: dict[str, AgentField]
    flags: list[str]
    embedded_instructions: int


class AgentUploadResponse(_Out):
    upload_handle: str


class AgentValidation(_Out):
    status: str
    rule_results: list[RuleResultOut]
    variance_explanations: list[str]


class AgentConfirmation(_In):
    vendor_name: str = Field(max_length=300)
    total_cents: Cents
    confirmed_by_human: bool


class AgentPostRequest(_In):
    confirmation: AgentConfirmation


class AgentPostResponse(_Out):
    bill_id: str
    system: str
    status: Literal["Posted"]
