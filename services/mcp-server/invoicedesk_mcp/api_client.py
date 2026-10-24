"""Client for the API agent facade (docs/api-contract.md, "Agent facade").

The MCP server never touches document bytes or text. It forwards a ``file_url`` or an
``upload_handle`` and receives *values only*. Every response is parsed into the narrow models
below (``extra="ignore"``), so a key the facade might add later (an OCR excerpt, a span) is
dropped here and can never reach the MCP client. That is invariant 2 enforced at this boundary
as well as in the API.

Each call carries a short-lived HS256 service JWT (``AGENT_JWT_SECRET``) with only the scopes
the calling tool needs, ``act.sub`` = the OAuth client id (audit actor ``mcp:<client>``) and
``sub`` = the end user the hosted authorization server named.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

import httpx
import jwt
from invoicedesk.money import Cents
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from invoicedesk_mcp.auth import Principal

__all__ = [
    "AgentApi",
    "AgentApiError",
    "ApiExtract",
    "ApiField",
    "ApiPost",
    "ApiRuleResult",
    "ApiValidation",
    "mint_service_token",
]


class _Narrow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ApiField(_Narrow):
    value: str | None = Field(None, max_length=500)
    calibrated_confidence: float | None = Field(None, ge=0.0, le=1.0)


class ApiExtract(_Narrow):
    document_id: str = Field(max_length=64)
    status: str = Field(max_length=32)
    fields: dict[str, ApiField] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)
    embedded_instructions: int = 0


class ApiRuleResult(_Narrow):
    rule_id: str = Field(max_length=8)
    name: str = Field(max_length=64)
    passed: bool
    severity: Literal["pass", "review", "flag"]
    message: str = Field(max_length=1000)
    fields: list[str] = Field(default_factory=list)
    #: Kept only to read R7's integer ``total_cents`` for the confirmation prompt.
    details: dict[str, Any] = Field(default_factory=dict)


class ApiValidation(_Narrow):
    status: str = Field(max_length=32)
    rule_results: list[ApiRuleResult] = Field(default_factory=list)
    variance_explanations: list[str] = Field(default_factory=list)


class ApiPost(_Narrow):
    bill_id: str = Field(max_length=128)
    system: str = Field(max_length=32)
    status: str = Field(max_length=32)


class AgentApiError(Exception):
    """A non-2xx answer (or a transport failure) from the facade, reduced to code + message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def mint_service_token(
    *,
    secret: str,
    audience: str,
    principal: Principal,
    scopes: tuple[str, ...],
    ttl_s: int,
    now: dt.datetime | None = None,
) -> str:
    issued = now or dt.datetime.now(dt.UTC)
    claims: dict[str, Any] = {
        "iss": "invoicedesk-mcp",
        "aud": audience,
        "scope": " ".join(scopes),
        "act": {"sub": principal.client_id},
        "iat": issued,
        "exp": issued + dt.timedelta(seconds=ttl_s),
        "jti": uuid.uuid4().hex,
    }
    if principal.subject:
        claims["sub"] = principal.subject
    return jwt.encode(claims, secret, algorithm="HS256")


class AgentApi:
    """Async facade client. ``http`` is injectable (tests pass an ASGI transport to the API)."""

    def __init__(
        self,
        *,
        base_url: str,
        secret: str,
        audience: str,
        token_ttl_s: int = 120,
        timeout_s: float = 90.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._secret = secret
        self._audience = audience
        self._ttl = token_ttl_s
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._owns_http = http is None
        self._timeout = timeout_s

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _headers(self, principal: Principal, scopes: tuple[str, ...]) -> dict[str, str]:
        token = mint_service_token(
            secret=self._secret,
            audience=self._audience,
            principal=principal,
            scopes=scopes,
            ttl_s=self._ttl,
        )
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def _call(
        self,
        method: str,
        path: str,
        *,
        principal: Principal,
        scopes: tuple[str, ...],
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            resp = await self._http.request(
                method,
                f"{self._base}{path}",
                json=json,
                headers=self._headers(principal, scopes),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise AgentApiError(504, "timeout", "InvoiceDesk did not answer in time.") from exc
        except httpx.HTTPError as exc:
            raise AgentApiError(502, "unavailable", "InvoiceDesk could not be reached.") from exc
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400:
            err = body.get("error") if isinstance(body, dict) else None
            code = str(err.get("code")) if isinstance(err, dict) else "error"
            message = str(err.get("message")) if isinstance(err, dict) else "Request failed."
            raise AgentApiError(resp.status_code, code[:64], message[:300])
        if not isinstance(body, dict):
            raise AgentApiError(502, "bad_response", "InvoiceDesk sent an unexpected response.")
        return body

    @staticmethod
    def _parse[M: _Narrow](model: type[M], body: dict[str, Any]) -> M:
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise AgentApiError(
                502, "bad_response", "InvoiceDesk sent an unexpected response."
            ) from exc

    async def extract(
        self, principal: Principal, *, file_url: str | None = None, upload_handle: str | None = None
    ) -> ApiExtract:
        payload = (
            {"file_url": file_url} if file_url is not None else {"upload_handle": upload_handle}
        )
        body = await self._call(
            "POST", "/agent/extract", principal=principal, scopes=("invoices:write",), json=payload
        )
        return self._parse(ApiExtract, body)

    async def validation(self, principal: Principal, document_id: str) -> ApiValidation:
        body = await self._call(
            "GET",
            f"/agent/documents/{document_id}/validation",
            principal=principal,
            scopes=("invoices:read",),
        )
        return self._parse(ApiValidation, body)

    async def post_bill(
        self, principal: Principal, document_id: str, *, vendor_name: str, total_cents: Cents
    ) -> ApiPost:
        body = await self._call(
            "POST",
            f"/agent/documents/{document_id}/post-bill",
            principal=principal,
            scopes=("bills:post",),
            json={
                "confirmation": {
                    "vendor_name": vendor_name,
                    "total_cents": total_cents,
                    "confirmed_by_human": True,
                }
            },
        )
        return self._parse(ApiPost, body)
