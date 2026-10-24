"""Test kit for the MCP server (used by ``services/mcp-server/tests``; the server never imports it).

The real API app (``invoicedesk_api``, offline fakes, throwaway Postgres) answers the facade
calls in-process; JWTs come from a test RSA key served as a JWKS over a mocked httpx transport.
Everything here is test-only.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import httpx2
import jwt
import mcp_types as types
from cryptography.hazmat.primitives.asymmetric import rsa
from invoicedesk_api.testkit import AGENT_SECRET, Env
from jwt.algorithms import RSAAlgorithm
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from invoicedesk_mcp.auth import JwksSource
from invoicedesk_mcp.server import McpApp, build_app
from invoicedesk_mcp.settings import McpSettings

ISSUER = "https://auth.invoicedesk.test"
JWKS_URL = f"{ISSUER}/oauth2/jwks"
RESOURCE = "https://mcp.invoicedesk.test/mcp"
MCP_ORIGIN = "https://mcp.invoicedesk.test"
CLIENT_ID = "https://claude.example/oauth/mcp-client-metadata.json"
ALL = "invoices:write invoices:read bills:post"
MODERN = "2026-07-28"


@dataclass
class Keys:
    private: rsa.RSAPrivateKey
    jwks: dict[str, Any]
    kid: str = "test-key-1"
    fetches: list[str] = field(default_factory=list)

    def token(
        self,
        scopes: str = ALL,
        *,
        aud: str | list[str] | None = RESOURCE,
        iss: str = ISSUER,
        client_id: str | None = CLIENT_ID,
        sub: str = "user_01JTEST",
        ttl: int = 600,
        kid: str | None = None,
        key: rsa.RSAPrivateKey | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        now = dt.datetime.now(dt.UTC)
        claims: dict[str, Any] = {
            "iss": iss,
            "sub": sub,
            "scope": scopes,
            "iat": now,
            "exp": now + dt.timedelta(seconds=ttl),
            "jti": uuid.uuid4().hex,
        }
        if aud is not None:
            claims["aud"] = aud
        if client_id is not None:
            claims["client_id"] = client_id
        claims.update(extra or {})
        return jwt.encode(
            claims, key or self.private, algorithm="RS256", headers={"kid": kid or self.kid}
        )


def make_keys() -> Keys:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    pub.update({"kid": "test-key-1", "use": "sig", "alg": "RS256"})
    return Keys(private=private, jwks={"keys": [pub]})


def jwks_http(keys: Keys) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        keys.fetches.append(str(request.url))
        if str(request.url) != JWKS_URL:
            return httpx.Response(404)
        return httpx.Response(200, json=keys.jwks)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class RecordingTransport(httpx.AsyncBaseTransport):
    """Wraps the in-process API transport and keeps every request and JSON response body the MCP
    server exchanged with the facade (for the "never receives raw document text" checks)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[dict[str, Any]] = []
        self.responses: list[Any] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
                "json": json.loads(body) if body else None,
            }
        )
        resp = await self.inner.handle_async_request(request)
        data = await resp.aread()
        if resp.headers.get("content-type", "").startswith("application/json"):
            self.responses.append(json.loads(data))
        return httpx.Response(resp.status_code, headers=resp.headers, content=data, request=request)


@dataclass
class Harness:
    built: McpApp
    maybe_env: Env | None
    keys: Keys
    api: RecordingTransport

    @property
    def env(self) -> Env:
        assert self.maybe_env is not None, "this harness has no API behind it"
        return self.maybe_env

    def http(self, headers: dict[str, str] | None = None) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.built.app),
            base_url=MCP_ORIGIN,
            headers=headers or {},
            timeout=180.0,
        )

    def client(
        self,
        token: str | None,
        *,
        elicit: Callable[..., Any] | None = None,
        mode: str = MODERN,
    ) -> Client:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return Client(
            streamable_http_client(RESOURCE, http_client=self.http(headers)),
            mode=mode,
            elicitation_callback=elicit,
            read_timeout_seconds=180.0,
        )

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        token: str | None,
        name: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx2.Response:
        """One raw 2026-07-28 JSON-RPC POST (envelope in ``_meta``, routing headers)."""
        p = dict(params or {})
        p["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
            **p.get("_meta", {}),
        }
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MODERN,
            "Mcp-Method": method,
        }
        if name is not None:
            h["Mcp-Name"] = name
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        async with self.http() as c:
            return await c.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": p},
                headers=h,
            )


def mcp_settings(**overrides: Any) -> McpSettings:
    base: dict[str, Any] = {
        "MCP_RESOURCE_URL": RESOURCE,
        "MCP_AUTH_ISSUER": ISSUER,
        "MCP_AUTH_JWKS_URL": JWKS_URL,
        "MCP_API_BASE_URL": "http://testserver/api/v1",
        "AGENT_JWT_SECRET": AGENT_SECRET,
        "MCP_JSON_RESPONSE": True,
        "INVOICEDESK_MODE": "offline",
    }
    base.update(overrides)
    return McpSettings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


@contextlib.asynccontextmanager
async def harness(env: Env | None, keys: Keys, **overrides: Any) -> AsyncIterator[Harness]:
    """The MCP app wired to the in-process API of ``env`` (or, with ``None``, to an API that
    answers 503, for tests that must never reach it)."""
    inner: httpx.AsyncBaseTransport = (
        httpx.ASGITransport(app=env.client.app)
        if env is not None
        else httpx.MockTransport(lambda _r: httpx.Response(503, json={"error": {"code": "down"}}))
    )
    recorder = RecordingTransport(inner)
    api_http = httpx.AsyncClient(transport=recorder, timeout=180.0)
    built = build_app(
        mcp_settings(**overrides),
        api_http=api_http,
        jwks=JwksSource(url=JWKS_URL, http=jwks_http(keys)),
    )
    async with built.app.router.lifespan_context(built.app):
        yield Harness(built=built, maybe_env=env, keys=keys, api=recorder)
    await api_http.aclose()


def accept(vendor: str, total: str, *, approve: bool = True) -> Callable[..., Any]:
    """An elicitation callback standing in for the human at the MCP client."""
    seen: list[types.ElicitRequestParams] = []

    async def cb(_ctx: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        seen.append(params)
        return types.ElicitResult(
            action="accept",
            content={"vendor_name": vendor, "total": total, "approve": approve},
        )

    cb.seen = seen  # type: ignore[attr-defined]
    return cb


def decline() -> Callable[..., Any]:
    async def cb(_ctx: Any, _params: types.ElicitRequestParams) -> types.ElicitResult:
        return types.ElicitResult(action="decline")

    return cb
