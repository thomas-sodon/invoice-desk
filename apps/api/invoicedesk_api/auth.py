"""Authentication (docs/api-contract.md, Auth).

* **Staff**: ``Authorization: Bearer <Supabase JWT>`` (HS256, ``SUPABASE_JWT_SECRET``, audience
  ``authenticated``, ``sub`` = ``staff_users.id``). In ``INVOICEDESK_MODE=offline`` only,
  ``X-Dev-User: <staff email>`` is accepted too.
* **Try-it visitor**: signed ``idsess`` cookie (itsdangerous, ``SESSION_SIGNING_SECRET``).
* **MCP server**: ``Authorization: Bearer <service JWT>`` (HS256, ``AGENT_JWT_SECRET``,
  audience ``AGENT_JWT_AUDIENCE``) whose ``scope`` lists ``invoices:write``, ``invoices:read``
  and/or ``bills:post`` and whose ``act`` names the MCP client (audit actor ``mcp:<client>``).
* **n8n**: ``X-InvoiceDesk-Signature: sha256=<hex HMAC of the raw body>``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import jwt
from fastapi import Depends, Request
from invoicedesk.status import Actor
from itsdangerous import BadSignature, URLSafeTimedSerializer

from invoicedesk_api.errors import ApiError
from invoicedesk_api.runtime import Runtime

__all__ = [
    "SESSION_COOKIE",
    "AgentPostDep",
    "AgentPrincipal",
    "AgentReadDep",
    "AgentWriteDep",
    "ApproverDep",
    "OptionalStaffDep",
    "RuntimeDep",
    "Staff",
    "StaffDep",
    "Visitor",
    "VisitorDep",
    "client_ip",
    "get_runtime",
    "ip_key",
    "optional_staff",
    "require_agent",
    "require_approver",
    "require_staff",
    "session_serializer",
    "verify_ingest_signature",
    "visitor",
]

SESSION_COOKIE = "idsess"
AGENT_SCOPES = frozenset({"invoices:write", "invoices:read", "bills:post"})
Role = Literal["clerk", "approver", "owner"]


@dataclass(frozen=True, slots=True)
class Staff:
    id: str
    tenant_id: str
    email: str
    display_name: str
    role: Role

    @property
    def actor(self) -> Actor:
        return Actor.user(self.id)

    @property
    def can_approve(self) -> bool:
        return self.role in ("approver", "owner")


@dataclass(frozen=True, slots=True)
class Visitor:
    session_id: str
    #: Day the session was issued; per-session upload counters are keyed on it.
    day: dt.date


@dataclass(frozen=True, slots=True)
class AgentPrincipal:
    client_id: str
    scopes: frozenset[str]
    tenant_id: str

    @property
    def actor(self) -> Actor:
        return Actor.mcp(self.client_id)


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


# ---------------------------------------------------------------------------- helpers
def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "unauthenticated", "Use an Authorization: Bearer token.")
    return token.strip()


def client_ip(request: Request, rt: Runtime) -> str:
    header = rt.api.client_ip_header
    if header:
        value = request.headers.get(header, "").strip()
        if value and "," not in value:
            return value
    if rt.api.trust_forwarded_for:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def ip_key(rt: Runtime, ip: str) -> str:
    """Salted hash of the IP: counters never store raw visitor addresses."""
    secret = rt.settings.session_signing_secret.get_secret_value().encode()
    return hmac.new(secret, f"ip:{ip}".encode(), hashlib.sha256).hexdigest()[:32]


def _staff_by(rt: Runtime, column: Literal["id", "email"], value: str) -> Staff | None:
    query = (
        "select id, tenant_id, email, display_name, role::text as role from staff_users"
        " where id::text = %s"
        if column == "id"
        else "select id, tenant_id, email, display_name, role::text as role from staff_users"
        " where lower(email) = lower(%s)"
    )
    with rt.db.tx() as conn:
        row = conn.execute(query, (value,)).fetchone()
    if row is None:
        return None
    return Staff(
        id=str(row["id"]),
        tenant_id=row["tenant_id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
    )


def _decode_staff_jwt(rt: Runtime, token: str) -> dict[str, Any]:
    secret = rt.settings.supabase_jwt_secret
    if secret is None:
        raise ApiError(401, "unauthenticated", "Staff sign-in is not configured.")
    try:
        return jwt.decode(
            token,
            secret.get_secret_value(),
            algorithms=["HS256"],
            audience=rt.api.supabase_jwt_audience,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise ApiError(401, "unauthenticated", "Your session is invalid or expired.") from exc


# ---------------------------------------------------------------------------- staff
def optional_staff(request: Request, rt: RuntimeDep) -> Staff | None:
    """The signed-in staff user, ``None`` for anonymous callers. A *bad* credential is a 401,
    never a silent downgrade to anonymous."""
    token = _bearer(request)
    staff: Staff | None = None
    if token is not None:
        claims = _decode_staff_jwt(rt, token)
        staff = _staff_by(rt, "id", str(claims["sub"]))
        if staff is None:
            raise ApiError(401, "unauthenticated", "This account is not an InvoiceDesk user.")
    else:
        dev = request.headers.get("x-dev-user")
        if dev:
            if not rt.offline:
                raise ApiError(401, "unauthenticated", "X-Dev-User works in offline mode only.")
            staff = _staff_by(rt, "email", dev.strip())
            if staff is None:
                raise ApiError(401, "unauthenticated", "Unknown dev user.")
    if staff is not None and staff.tenant_id != rt.tenant:
        raise ApiError(403, "forbidden", "This account belongs to another organization.")
    return staff


def require_staff(staff: Annotated[Staff | None, Depends(optional_staff)]) -> Staff:
    if staff is None:
        raise ApiError(401, "unauthenticated", "Sign in to do that.")
    return staff


def require_approver(staff: Annotated[Staff, Depends(require_staff)]) -> Staff:
    if not staff.can_approve:
        raise ApiError(403, "forbidden", "Only an approver or owner can do that.")
    return staff


# ---------------------------------------------------------------------------- visitors
def session_serializer(rt: Runtime) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        rt.settings.session_signing_secret.get_secret_value(), salt="invoicedesk.tryit.session"
    )


def visitor(request: Request, rt: RuntimeDep) -> Visitor | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = session_serializer(rt).loads(raw, max_age=rt.api.tryit_session_ttl_s)
        return Visitor(session_id=str(data["sid"]), day=dt.date.fromisoformat(str(data["day"])))
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------- agents
def _decode_agent(rt: Runtime, token: str) -> AgentPrincipal:
    secret = rt.api.agent_jwt_secret
    if secret is None:
        raise ApiError(401, "unauthenticated", "The agent facade is not configured.")
    try:
        claims = jwt.decode(
            token,
            secret.get_secret_value(),
            algorithms=["HS256"],
            audience=rt.api.agent_jwt_audience,
            options={"require": ["exp", "scope", "act"]},
        )
    except jwt.PyJWTError as exc:
        raise ApiError(401, "unauthenticated", "Invalid or expired service token.") from exc
    act = claims.get("act")
    client = act.get("sub") if isinstance(act, dict) else act  # RFC 8693 actor claim or a string
    if not isinstance(client, str) or not client.strip() or len(client) > 200:
        raise ApiError(401, "unauthenticated", "The service token names no MCP client (act).")
    scope = claims.get("scope")
    scopes = frozenset(str(scope).split()) & AGENT_SCOPES
    tenant = str(claims.get("tenant_id") or rt.tenant)
    if tenant != rt.tenant:
        raise ApiError(403, "forbidden", "The service token is for another organization.")
    return AgentPrincipal(client_id=client.strip(), scopes=scopes, tenant_id=tenant)


def require_agent(scope: str) -> Any:
    def dep(request: Request, rt: RuntimeDep) -> AgentPrincipal:
        token = _bearer(request)
        if token is None:
            raise ApiError(401, "unauthenticated", "A service token is required.")
        agent = _decode_agent(rt, token)
        if scope not in agent.scopes:
            raise ApiError(403, "forbidden", f"The service token lacks the {scope} scope.")
        return agent

    return dep


# ---------------------------------------------------------------------------- n8n
def verify_ingest_signature(rt: Runtime, body: bytes, header: str | None) -> None:
    secret = rt.settings.ingest_webhook_secret.get_secret_value().encode()
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not header or not hmac.compare_digest(header.strip().lower(), expected):
        raise ApiError(401, "unauthenticated", "Bad or missing X-InvoiceDesk-Signature.")


StaffDep = Annotated[Staff, Depends(require_staff)]
OptionalStaffDep = Annotated[Staff | None, Depends(optional_staff)]
ApproverDep = Annotated[Staff, Depends(require_approver)]
VisitorDep = Annotated[Visitor | None, Depends(visitor)]
AgentWriteDep = Annotated[AgentPrincipal, Depends(require_agent("invoices:write"))]
AgentReadDep = Annotated[AgentPrincipal, Depends(require_agent("invoices:read"))]
AgentPostDep = Annotated[AgentPrincipal, Depends(require_agent("bills:post"))]
