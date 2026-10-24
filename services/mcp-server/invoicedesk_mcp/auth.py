"""OAuth resource-server side of the MCP server (DESIGN §4.4, §5.5; MCP 2026-07-28 auth).

* **Protected Resource Metadata** (RFC 9728) names this resource and the hosted authorization
  server. Clients register with that server via CIMD; this module issues nothing.
* **Bearer JWTs** are verified against the issuer's JWKS (fetched with httpx, cached, refetched
  on an unknown ``kid``): asymmetric algorithms only, ``iss`` must be the configured issuer and
  ``aud`` must name this server's URL (RFC 8707). A token issued for another resource is refused.
* **Per-tool scopes**: ``extract_invoice`` needs ``invoices:write``, ``validate_invoice``
  ``invoices:read`` and ``post_bill`` ``bills:post``. The HTTP gate reads the ``Mcp-Method`` /
  ``Mcp-Name`` headers and answers ``403 insufficient_scope`` before dispatch; each tool checks
  again, so a request without those headers is still covered.
* Every 401/403 carries ``WWW-Authenticate: Bearer ... resource_metadata="<PRM URL>"``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = [
    "ALL_SCOPES",
    "TOOL_SCOPES",
    "JwksSource",
    "JwtTokenVerifier",
    "McpAuthGate",
    "Principal",
    "build_prm_document",
    "current_principal",
    "prm_paths",
    "prm_url",
    "www_authenticate",
]

logger = logging.getLogger(__name__)

TOOL_SCOPES: Mapping[str, str] = {
    "extract_invoice": "invoices:write",
    "validate_invoice": "invoices:read",
    "post_bill": "bills:post",
}
ALL_SCOPES: tuple[str, ...] = ("invoices:write", "invoices:read", "bills:post")
_PRM_WELL_KNOWN = "/.well-known/oauth-protected-resource"
_MAX_CLIENT_ID = 200


# ----------------------------------------------------------------------------- PRM (RFC 9728)
def prm_url(resource_url: str) -> str:
    """RFC 9728 §3.1: the well-known segment goes between the host and the resource path."""
    scheme, rest = resource_url.split("://", 1)
    netloc, _, path = rest.partition("/")
    return f"{scheme}://{netloc}{_PRM_WELL_KNOWN}" + (f"/{path}" if path else "")


def prm_paths(resource_url: str) -> list[str]:
    """Served at the path-inserted location and at the bare well-known path (DESIGN §5.5)."""
    path = "/" + prm_url(resource_url).split("://", 1)[1].partition("/")[2]
    return list(dict.fromkeys([path, _PRM_WELL_KNOWN]))


def build_prm_document(
    *,
    resource_url: str,
    issuer: str,
    resource_name: str,
    resource_documentation: str | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "resource": resource_url,
        "authorization_servers": [issuer],
        "scopes_supported": list(ALL_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": resource_name,
    }
    if resource_documentation:
        doc["resource_documentation"] = resource_documentation
    return doc


def www_authenticate(
    resource_url: str, *, error: str | None = None, description: str | None = None, scope: str = ""
) -> str:
    parts: list[str] = []
    if error:
        parts.append(f'error="{error}"')
    if description:
        parts.append(f'error_description="{description}"')
    if scope:
        parts.append(f'scope="{scope}"')
    parts.append(f'resource_metadata="{prm_url(resource_url)}"')
    return "Bearer " + ", ".join(parts)


# ----------------------------------------------------------------------------- JWKS
class JwksSource:
    """The issuer's signing keys: fetched over httpx (or read from a file offline), cached.

    An unknown ``kid`` triggers one refetch, rate-limited so a stream of junk tokens cannot turn
    the server into a JWKS hammer."""

    def __init__(
        self,
        *,
        url: str | None = None,
        file: Path | None = None,
        http: httpx.AsyncClient | None = None,
        ttl_s: float = 600.0,
        min_refetch_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (url is None) == (file is None):
            raise ValueError("JwksSource takes exactly one of url= or file=")
        self._url = url
        self._file = file
        self._http = http
        self._ttl = ttl_s
        self._min_refetch = min_refetch_s
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._anonymous: list[jwt.PyJWK] = []
        self._fetched_at: float | None = None
        self._lock = anyio.Lock()

    async def _load(self) -> dict[str, Any]:
        if self._file is not None:
            return json.loads(await anyio.Path(self._file).read_text())
        assert self._url is not None
        client = self._http or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.get(self._url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()
        finally:
            if self._http is None:
                await client.aclose()

    async def _refresh(self) -> None:
        data = await self._load()
        keys: dict[str, jwt.PyJWK] = {}
        anonymous: list[jwt.PyJWK] = []
        for raw in data.get("keys", []) if isinstance(data, dict) else []:
            if not isinstance(raw, dict) or raw.get("use", "sig") != "sig":
                continue
            if raw.get("kty") == "oct":  # never accept a symmetric key from a JWKS
                continue
            try:
                key = jwt.PyJWK.from_dict(raw)
            except jwt.PyJWTError:
                continue
            if raw.get("kid"):
                keys[str(raw["kid"])] = key
            else:
                anonymous.append(key)
        self._keys, self._anonymous = keys, anonymous
        self._fetched_at = self._clock()

    async def get(self, kid: str | None) -> list[jwt.PyJWK]:
        async with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or now - self._fetched_at > self._ttl
            missing = kid is not None and kid not in self._keys
            can_refetch = self._fetched_at is None or now - self._fetched_at >= self._min_refetch
            if stale or (missing and can_refetch):
                try:
                    await self._refresh()
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    logger.warning("JWKS fetch failed: %s", type(exc).__name__)
                    if self._fetched_at is None:
                        return []
            if kid is not None:
                return [self._keys[kid]] if kid in self._keys else []
            return [*self._keys.values(), *self._anonymous]


# ----------------------------------------------------------------------------- verifier
def _scopes(claims: Mapping[str, Any]) -> list[str]:
    for key in ("scope", "scp", "permissions"):
        v = claims.get(key)
        if isinstance(v, str):
            return [s for s in v.split() if s]
        if isinstance(v, list):
            return [str(s) for s in v if isinstance(s, str) and s]
    return []


def _client_id(claims: Mapping[str, Any]) -> str | None:
    for key in ("client_id", "azp", "cid"):
        v = claims.get(key)
        if isinstance(v, str) and v.strip() and len(v) <= _MAX_CLIENT_ID:
            return v.strip()
    return None


def _same_resource(a: str, b: str) -> bool:
    return a.rstrip("/").casefold() == b.rstrip("/").casefold()


class JwtTokenVerifier:
    """``mcp`` ``TokenVerifier`` for the hosted authorization server's access tokens."""

    def __init__(
        self,
        *,
        jwks: JwksSource,
        issuer: str,
        resource_url: str,
        algorithms: list[str],
        leeway_s: int = 30,
    ) -> None:
        if not algorithms:
            raise ValueError("at least one asymmetric JWT algorithm is required")
        self._jwks = jwks
        self._issuer = issuer
        self._resource = resource_url
        self._algorithms = algorithms
        self._leeway = leeway_s

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None
        alg = header.get("alg")
        if alg not in self._algorithms:
            return None
        kid = header.get("kid")
        for key in await self._jwks.get(kid if isinstance(kid, str) else None):
            try:
                claims = jwt.decode(
                    token,
                    key=key,
                    algorithms=self._algorithms,
                    issuer=self._issuer,
                    # RFC 8707: the token must be issued for this resource. PyJWT accepts an
                    # ``aud`` list containing it; the slash-variant covers issuer normalization.
                    audience=[self._resource, self._resource + "/"],
                    leeway=self._leeway,
                    options={"require": ["exp", "iss", "aud"]},
                )
            except jwt.InvalidSignatureError:
                continue
            except jwt.PyJWTError:
                return None
            return self._access_token(token, claims)
        return None

    def _access_token(self, token: str, claims: dict[str, Any]) -> AccessToken | None:
        resource = claims.get("resource")
        if resource is not None and (
            not isinstance(resource, str) or not _same_resource(resource, self._resource)
        ):
            return None
        client_id = _client_id(claims)
        if client_id is None:
            return None
        sub = claims.get("sub")
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=_scopes(claims),
            expires_at=int(claims["exp"]),
            resource=self._resource,
            subject=sub if isinstance(sub, str) else None,
            claims={k: claims[k] for k in ("iss", "sub", "aud", "org_id") if k in claims},
        )


# ----------------------------------------------------------------------------- principal
@dataclass(frozen=True)
class Principal:
    client_id: str
    subject: str | None
    scopes: frozenset[str]

    @property
    def audit_actor(self) -> str:
        return f"mcp:{self.client_id}"


def current_principal() -> Principal | None:
    token = get_access_token()
    if token is None:
        return None
    return Principal(
        client_id=token.client_id, subject=token.subject, scopes=frozenset(token.scopes)
    )


# ----------------------------------------------------------------------------- HTTP gate
class McpAuthGate:
    """ASGI gate for the MCP endpoint: 401 without a valid token, 403 for a missing tool scope.

    Runs after Starlette's ``AuthenticationMiddleware`` (which uses :class:`JwtTokenVerifier`
    through the SDK's ``BearerAuthBackend``, with RFC 8707 resource checking on)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        resource_url: str,
        on_request: Callable[[Scope], Awaitable[None]] | None = None,
    ) -> None:
        self.app = app
        self.resource_url = resource_url
        self.on_request = on_request

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        user = scope.get("user")
        if not isinstance(user, AuthenticatedUser):
            await self._deny(
                scope,
                receive,
                send,
                status=401,
                error="invalid_token",
                description="Authentication required",
            )
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method = headers.get("mcp-method")
        name = headers.get("mcp-name")
        if method == "tools/call" and name is not None and name in TOOL_SCOPES:
            needed = TOOL_SCOPES[name]
            if needed not in user.scopes:
                await self._deny(
                    scope,
                    receive,
                    send,
                    status=403,
                    error="insufficient_scope",
                    description=f"The {name} tool needs the {needed} scope",
                    scope_hint=needed,
                )
                return
        if self.on_request is not None:
            await self.on_request(scope)
        await self.app(scope, receive, send)

    async def _deny(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status: int,
        error: str,
        description: str,
        scope_hint: str = "",
    ) -> None:
        resp = JSONResponse(
            {"error": error, "error_description": description},
            status_code=status,
            headers={
                "WWW-Authenticate": www_authenticate(
                    self.resource_url, error=error, description=description, scope=scope_hint
                )
            },
        )
        await resp(scope, receive, send)


def prm_endpoint(document: dict[str, Any]) -> Callable[[Request], Awaitable[Response]]:
    async def handler(request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=_CORS)
        return JSONResponse(document, headers={"Cache-Control": "public, max-age=3600", **_CORS})

    return handler


_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "mcp-protocol-version",
}
