"""The three MCP tools (DESIGN §5.5, M16, US-13).

* ``extract_invoice`` (``invoices:write``): file_url or upload_handle -> a document_id handle,
  field values, calibrated confidences and flags.
* ``validate_invoice`` (``invoices:read``): read-only status, rule results, variance sentences.
* ``post_bill`` (``bills:post``): ReadyToPay only, never Flagged; a human confirms the vendor
  and total first (MRTR ``input_required`` elicitation).

Stateless: there is no session, so every call names its document explicitly. Deterministic code
decides (invariant 1): the agent can ask, but only the API's state machine moves a document, and
the human's typed confirmation is checked against the stored vendor and total by the API.

This module deliberately has no ``from __future__ import annotations``: the SDK resolves the
``Annotated[..., Resolve(...)]`` hints of the closures below with ``get_type_hints``, which needs
real (not string) annotations for names local to :func:`register_tools`.
"""

import logging
import re
from typing import Annotated, Literal

from invoicedesk.money import MoneyParseError, format_cents, parse_money_to_cents
from mcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
    ElicitationResult,
)
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp_types import ToolAnnotations
from mcp_types.version import is_version_at_least
from pydantic import BaseModel, ConfigDict, Field

from invoicedesk_mcp.api_client import AgentApi, AgentApiError, ApiValidation
from invoicedesk_mcp.auth import TOOL_SCOPES, Principal, current_principal

__all__ = [
    "DESCRIPTIONS",
    "ConfirmPosting",
    "ExtractInvoiceResult",
    "PostBillResult",
    "RuleResultOut",
    "ValidateInvoiceResult",
    "register_tools",
]

logger = logging.getLogger(__name__)

#: First protocol revision with multi-round-trip requests (``input_required``).
MRTR_VERSION = "2026-07-28"

DESCRIPTIONS: dict[str, str] = {
    "extract_invoice": (
        "Read one vendor invoice (PDF or image) from an https file_url or an upload_handle. "
        "Screens it for hidden instructions, extracts header fields and returns a document_id, "
        "field values, calibrated confidence per field and risk flags. Validation runs "
        "automatically; call validate_invoice next."
    ),
    "validate_invoice": (
        "Read-only. For a document_id, return its status (ReadyToPay, NeedsReview, Flagged, "
        "Posted and so on), the ten deterministic rule results and plain-English variance "
        "explanations against the approved estimate."
    ),
    "post_bill": (
        "Post a ReadyToPay invoice to QuickBooks Online as a bill. Refuses any other status and "
        "always refuses Flagged invoices. Asks the human to confirm the vendor and total before "
        "posting. Safe to retry: a document is never posted twice."
    ),
}

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DocumentId = Annotated[
    str,
    Field(
        description="The document_id returned by extract_invoice.",
        min_length=36,
        max_length=36,
    ),
]


# ----------------------------------------------------------------------------- outputs
class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractInvoiceResult(_Out):
    document_id: str
    status: str
    fields: dict[str, str | None] = Field(
        description="Extracted header values. Money fields are integer cents."
    )
    calibrated_confidence: dict[str, float | None] = Field(
        description="Calibrated probability (0-1) that each field value is correct."
    )
    flags: list[str] = Field(description="Risk flags such as duplicate or bank_change.")
    embedded_instruction_count: int = Field(
        description="How many instructions aimed at software were found in the document. "
        "They are never followed or shown."
    )


class RuleResultOut(_Out):
    rule_id: str
    name: str
    passed: bool
    severity: Literal["pass", "review", "flag"]
    message: str
    fields: list[str]


class ValidateInvoiceResult(_Out):
    document_id: str
    status: str
    rule_results: list[RuleResultOut]
    variance_explanations: list[str]


class PostBillResult(_Out):
    bill_id: str
    system: str
    status: str


class ConfirmPosting(BaseModel):
    """The human's confirmation form (elicitation). Typed, not pre-filled: the API compares it
    with the stored vendor and total, so a mismatch is refused."""

    vendor_name: str = Field(
        title="Vendor name", description="The vendor name as printed on the invoice."
    )
    total: str = Field(title="Invoice total", description="The invoice total, e.g. 1,240.00.")
    approve: bool = Field(
        title="Post this bill",
        description="I checked this invoice and want it posted to QuickBooks Online.",
    )


class _Postable(BaseModel):
    document_id: str
    status: str
    total_cents: int | None


# ----------------------------------------------------------------------------- helpers
def _principal(tool: str) -> Principal:
    principal = current_principal()
    if principal is None:
        raise ToolError("Not authorized: this server needs an OAuth access token.")
    needed = TOOL_SCOPES[tool]
    if needed not in principal.scopes:
        raise ToolError(f"Not authorized: {tool} needs the {needed} scope.")
    return principal


def _check_document_id(document_id: str) -> str:
    if not _UUID.match(document_id):
        raise ToolError("document_id must be the UUID returned by extract_invoice.")
    return document_id.lower()


def _api_failure(exc: AgentApiError, *, what: str) -> ToolError:
    if exc.status == 404:
        return ToolError("No such document. Use the document_id returned by extract_invoice.")
    if exc.code in ("rejected_input", "validation_error"):
        return ToolError(f"{what} failed: {exc.message}")
    if exc.status in (401, 403):
        return ToolError(f"{what} was not authorized by InvoiceDesk.")
    if exc.status == 429:
        return ToolError(f"{what} is rate limited; try again later.")
    return ToolError(f"{what} failed: InvoiceDesk is unavailable ({exc.code}). Try again later.")


def _log_routing(ctx: Context, tool: str, principal: Principal) -> None:
    headers = ctx.headers or {}
    logger.info(
        "tool=%s client=%s mcp_method=%s mcp_name=%s",
        tool,
        principal.client_id,
        headers.get("mcp-method"),
        headers.get("mcp-name"),
    )


def _refusal(status: str) -> ToolError | None:
    if status == "Flagged":
        return ToolError(
            "Refused: this invoice is Flagged (for example a bank-detail change, a duplicate or "
            "hidden instructions). Only two human approvers in InvoiceDesk can release it; an "
            "agent can never post it."
        )
    if status not in ("ReadyToPay", "Posted"):
        return ToolError(
            f"Refused: only ReadyToPay invoices can be posted; this one is {status}. "
            "A person must resolve it in InvoiceDesk first."
        )
    return None


def _total_from_rules(v: ApiValidation) -> int | None:
    for r in v.rule_results:
        if r.rule_id == "R7":
            total = r.details.get("total_cents")
            if isinstance(total, int) and not isinstance(total, bool):
                return total
    return None


# ----------------------------------------------------------------------------- tools
def register_tools(server: MCPServer, api: AgentApi) -> None:
    """Register the three tools on ``server``; ``api`` is the facade client."""
    _register_extract(server, api)
    _register_validate(server, api)
    _register_post_bill(server, api)


def _register_extract(server: MCPServer, api: AgentApi) -> None:

    @server.tool(
        name="extract_invoice",
        title="Extract invoice",
        description=DESCRIPTIONS["extract_invoice"],
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def extract_invoice(
        ctx: Context,
        file_url: Annotated[
            str | None,
            Field(description="https URL of a PDF, JPEG, PNG or HEIC invoice.", max_length=2048),
        ] = None,
        upload_handle: Annotated[
            str | None,
            Field(description="Handle from an InvoiceDesk agent upload.", max_length=512),
        ] = None,
    ) -> ExtractInvoiceResult:
        principal = _principal("extract_invoice")
        _log_routing(ctx, "extract_invoice", principal)
        if (file_url is None) == (upload_handle is None):
            raise ToolError("Give exactly one of file_url or upload_handle.")
        if file_url is not None and not file_url.lower().startswith(("https://", "http://")):
            raise ToolError("file_url must be an https URL.")
        try:
            out = await api.extract(principal, file_url=file_url, upload_handle=upload_handle)
        except AgentApiError as exc:
            raise _api_failure(exc, what="Extraction") from exc
        return ExtractInvoiceResult(
            document_id=out.document_id,
            status=out.status,
            fields={k: f.value for k, f in out.fields.items()},
            calibrated_confidence={k: f.calibrated_confidence for k, f in out.fields.items()},
            flags=list(out.flags),
            embedded_instruction_count=out.embedded_instructions,
        )


def _register_validate(server: MCPServer, api: AgentApi) -> None:
    @server.tool(
        name="validate_invoice",
        title="Validate invoice",
        description=DESCRIPTIONS["validate_invoice"],
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    async def validate_invoice(document_id: _DocumentId, ctx: Context) -> ValidateInvoiceResult:
        principal = _principal("validate_invoice")
        _log_routing(ctx, "validate_invoice", principal)
        doc_id = _check_document_id(document_id)
        try:
            v = await api.validation(principal, doc_id)
        except AgentApiError as exc:
            raise _api_failure(exc, what="Validation") from exc
        return ValidateInvoiceResult(
            document_id=doc_id,
            status=v.status,
            rule_results=[
                RuleResultOut(
                    rule_id=r.rule_id,
                    name=r.name,
                    passed=r.passed,
                    severity=r.severity,
                    message=r.message,
                    fields=list(r.fields),
                )
                for r in v.rule_results
            ],
            variance_explanations=list(v.variance_explanations),
        )


def _register_post_bill(server: MCPServer, api: AgentApi) -> None:
    """post_bill: precheck (resolver) -> human confirmation (elicitation) -> post."""

    async def ready_for_posting(document_id: str, ctx: Context) -> _Postable:
        """Refuse before asking the human anything when the document cannot be posted."""
        principal = _principal("post_bill")
        _log_routing(ctx, "post_bill", principal)
        doc_id = _check_document_id(document_id)
        try:
            # Internal read of the same document, used only to refuse early; the facade's
            # post-bill endpoint enforces the same state rules again.
            v = await api.validation(principal, doc_id)
        except AgentApiError as exc:
            raise _api_failure(exc, what="Posting") from exc
        refusal = _refusal(v.status)
        if refusal is not None:
            raise refusal
        return _Postable(document_id=doc_id, status=v.status, total_cents=_total_from_rules(v))

    async def ask_confirmation(
        postable: Annotated[_Postable, Resolve(ready_for_posting)],
        ctx: Context,
    ) -> Elicit[ConfirmPosting]:
        if not is_version_at_least(ctx.protocol_version or "", MRTR_VERSION):
            # This server is stateless, so a pre-2026 client offers no back-channel for an
            # elicitation request; without a human confirmation nothing is posted.
            raise ToolError(
                "Not posted: post_bill needs a human confirmation, which requires an MCP "
                f"client on protocol {MRTR_VERSION} or later. Post it in InvoiceDesk instead."
            )
        read = (
            f" InvoiceDesk read a total of {format_cents(postable.total_cents)}."
            if postable.total_cents is not None
            else ""
        )
        return Elicit(
            f"Confirm posting invoice {postable.document_id} to QuickBooks Online as a bill."
            f"{read} Type the vendor name and total exactly as printed on the invoice, then "
            "tick 'Post this bill'.",
            ConfirmPosting,
        )

    @server.tool(
        name="post_bill",
        title="Post bill",
        description=DESCRIPTIONS["post_bill"],
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
        ),
    )
    async def post_bill(
        document_id: _DocumentId,
        postable: Annotated[_Postable, Resolve(ready_for_posting)],
        confirmation: Annotated[ElicitationResult[ConfirmPosting], Resolve(ask_confirmation)],
    ) -> PostBillResult:
        principal = _principal("post_bill")
        if isinstance(confirmation, DeclinedElicitation | CancelledElicitation):
            raise ToolError("Not posted: the person declined to confirm the bill.")
        assert isinstance(confirmation, AcceptedElicitation)
        answer = confirmation.data
        if not answer.approve:
            raise ToolError("Not posted: the person did not tick 'Post this bill'.")
        try:
            total_cents = parse_money_to_cents(answer.total.strip())
        except MoneyParseError as exc:
            raise ToolError("Not posted: the confirmed total is not an amount.") from exc
        try:
            out = await api.post_bill(
                principal,
                postable.document_id,
                vendor_name=answer.vendor_name.strip(),
                total_cents=total_cents,
            )
        except AgentApiError as exc:
            if exc.code == "confirmation_mismatch":
                raise ToolError(
                    "Not posted: the vendor or total the person confirmed does not match the "
                    "invoice. Check the invoice in InvoiceDesk."
                ) from exc
            if exc.code == "illegal_transition" or exc.status == 409:
                raise ToolError(f"Refused: {exc.message}") from exc
            raise _api_failure(exc, what="Posting") from exc
        return PostBillResult(bill_id=out.bill_id, system=out.system, status=out.status)
