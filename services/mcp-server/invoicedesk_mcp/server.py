"""InvoiceDesk remote MCP server (M16): Streamable HTTP, stateless, OAuth PRM.

``invoicedesk-mcp`` (``main``) serves:

* ``POST <resource path>`` (default ``/mcp``): MCP over Streamable HTTP. Protocol 2026-07-28
  requests are self-contained (no ``initialize``, no ``Mcp-Session-Id``); older clients are
  served in the SDK's stateless mode. Bearer token required; see :mod:`invoicedesk_mcp.auth`.
* ``GET /.well-known/oauth-protected-resource[/<path>]``: RFC 9728 metadata.
* ``GET /healthz``.

``tools/list`` results carry ``ttlMs``/``cacheScope`` hints. ``post_bill`` asks the human to
confirm through an MRTR ``input_required`` round-trip (the SDK's elicitation resolver). A
pre-2026-07-28 client has no back-channel on this stateless server, so ``post_bill`` refuses it
rather than post without a human confirmation.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.request_state import RequestStateSecurity
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from invoicedesk_mcp import __version__
from invoicedesk_mcp.api_client import AgentApi
from invoicedesk_mcp.auth import (
    JwksSource,
    JwtTokenVerifier,
    McpAuthGate,
    build_prm_document,
    prm_endpoint,
    prm_paths,
)
from invoicedesk_mcp.settings import McpSettings, get_mcp_settings
from invoicedesk_mcp.tools import register_tools

__all__ = ["McpApp", "build_app", "build_mcp_server", "main"]

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "InvoiceDesk checks vendor invoices against work orders, estimates and the vendor list. "
    "Call extract_invoice, then validate_invoice, then post_bill only for ReadyToPay invoices. "
    "Flagged invoices always need people; never try to work around a refusal."
)


class ConfigError(RuntimeError):
    """The server is not configured well enough to start safely."""


def build_mcp_server(settings: McpSettings, api: AgentApi) -> MCPServer:
    if settings.request_state_keys is not None:
        keys = [
            k.strip()
            for k in settings.request_state_keys.get_secret_value().split(",")
            if k.strip()
        ]
        security = RequestStateSecurity(
            keys=keys, ttl=settings.request_state_ttl_s, audience=settings.resource_url
        )
    else:
        security = RequestStateSecurity.ephemeral(
            ttl=settings.request_state_ttl_s, audience=settings.resource_url
        )
    server = MCPServer(
        "invoicedesk",
        title="InvoiceDesk",
        instructions=INSTRUCTIONS,
        version=__version__,
        request_state_security=security,
        cache_hints={"tools/list": CacheHint(ttl_ms=settings.tools_list_ttl_ms, scope="public")},
    )
    register_tools(server, api)
    return server


@dataclass
class McpApp:
    """The ASGI app plus the pieces tests and ``main`` need to reach."""

    app: Starlette
    server: MCPServer
    api: AgentApi
    settings: McpSettings


def _jwks_source(settings: McpSettings, http: httpx.AsyncClient | None) -> JwksSource:
    if settings.auth_jwks_file is not None:
        if settings.mode != "offline":
            raise ConfigError("MCP_AUTH_JWKS_FILE is for offline development only.")
        return JwksSource(file=settings.auth_jwks_file, ttl_s=settings.jwks_cache_ttl_s)
    url = settings.jwks_url
    if url is None:
        raise ConfigError("Set MCP_AUTH_ISSUER (and optionally MCP_AUTH_JWKS_URL).")
    if settings.mode == "live" and not url.startswith("https://"):
        raise ConfigError("MCP_AUTH_JWKS_URL must be https in live mode.")
    return JwksSource(url=url, http=http, ttl_s=settings.jwks_cache_ttl_s)


def build_app(
    settings: McpSettings | None = None,
    *,
    api_http: httpx.AsyncClient | None = None,
    jwks_http: httpx.AsyncClient | None = None,
    jwks: JwksSource | None = None,
) -> McpApp:
    """Compose the server. ``api_http``/``jwks_http``/``jwks`` are injection points for tests."""
    s = settings or get_mcp_settings()
    if not s.auth_issuer:
        raise ConfigError("MCP_AUTH_ISSUER is required: the hosted authorization server's issuer.")
    if s.agent_jwt_secret is None:
        raise ConfigError("AGENT_JWT_SECRET is required to call the API agent facade.")
    if s.mode == "live" and not s.resource_url.startswith("https://"):
        raise ConfigError("MCP_RESOURCE_URL must be https in live mode.")

    api = AgentApi(
        base_url=s.api_base_url,
        secret=s.agent_jwt_secret.get_secret_value(),
        audience=s.agent_jwt_audience,
        token_ttl_s=s.service_token_ttl_s,
        timeout_s=s.api_timeout_s,
        http=api_http,
    )
    server = build_mcp_server(s, api)
    verifier = JwtTokenVerifier(
        jwks=jwks or _jwks_source(s, jwks_http),
        issuer=s.auth_issuer,
        resource_url=s.resource_url,
        algorithms=s.algorithms,
        leeway_s=s.auth_leeway_s,
    )
    # Creates the (stateless) session manager; the SDK's own Starlette wrapper is not used
    # because the per-tool scope gate has to sit between authentication and the transport.
    server.streamable_http_app(
        streamable_http_path=s.mcp_path,
        json_response=s.json_response,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[s.resource_netloc, *s.extra_hosts],
            allowed_origins=s.origins,
        ),
    )
    session_manager = server.session_manager
    prm = build_prm_document(
        resource_url=s.resource_url,
        issuer=s.auth_issuer,
        resource_name=s.resource_name,
        resource_documentation=s.resource_documentation,
    )

    async def healthz(_: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__})

    routes = [
        *(
            Route(p, endpoint=prm_endpoint(prm), methods=["GET", "OPTIONS"])
            for p in prm_paths(s.resource_url)
        ),
        Route("/healthz", endpoint=healthz, methods=["GET"]),
        Route(
            s.mcp_path,
            endpoint=McpAuthGate(
                StreamableHTTPASGIApp(session_manager), resource_url=s.resource_url
            ),
        ),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            try:
                yield
            finally:
                await api.aclose()

    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(verifier, resource_server_url=AnyHttpUrl(s.resource_url)),
                on_error=_auth_error,
            ),
            Middleware(AuthContextMiddleware),
        ],
        lifespan=lifespan,
    )
    return McpApp(app=app, server=server, api=api, settings=s)


def _auth_error(
    _conn: object, _exc: Exception
) -> Response:  # pragma: no cover - backend never raises
    return JSONResponse({"error": "invalid_token"}, status_code=401)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(
        prog="invoicedesk-mcp", description="InvoiceDesk remote MCP server"
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = get_mcp_settings()
    try:
        built = build_app(s)
    except ConfigError as exc:
        raise SystemExit(f"invoicedesk-mcp: {exc}") from exc
    uvicorn.run(
        built.app,
        host=args.host or s.host,
        port=args.port or s.port,
        proxy_headers=True,
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
