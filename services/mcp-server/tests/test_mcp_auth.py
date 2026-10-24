"""OAuth resource server: PRM (RFC 9728), JWKS-verified JWTs, RFC 8707 audience, tool scopes."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from invoicedesk_mcp.auth import JwksSource, JwtTokenVerifier, prm_paths, prm_url
from invoicedesk_mcp.server import ConfigError, build_app
from invoicedesk_mcp.testkit import (
    ALL,
    ISSUER,
    JWKS_URL,
    MCP_ORIGIN,
    RESOURCE,
    Keys,
    harness,
    jwks_http,
    mcp_settings,
)

pytestmark = pytest.mark.asyncio

PRM = f"{MCP_ORIGIN}/.well-known/oauth-protected-resource/mcp"


def _www(resp: Any) -> str:
    return resp.headers.get("www-authenticate", "")


async def test_protected_resource_metadata(keys: Keys) -> None:
    assert prm_url(RESOURCE) == PRM
    assert prm_paths(RESOURCE) == [
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ]
    async with harness(None, keys) as h, h.http() as c:
        for path in prm_paths(RESOURCE):
            r = await c.get(path)
            assert r.status_code == 200
            doc = r.json()
            assert doc["resource"] == RESOURCE
            assert doc["authorization_servers"] == [ISSUER]
            assert doc["scopes_supported"] == ["invoices:write", "invoices:read", "bills:post"]
            assert doc["bearer_methods_supported"] == ["header"]
            assert r.headers["access-control-allow-origin"] == "*"
        health = await c.get("/healthz")
        assert health.status_code == 200 and health.json()["ok"] is True


async def test_missing_and_invalid_tokens_get_401_with_resource_metadata(keys: Keys) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    bad_tokens: dict[str, str | None] = {
        "missing": None,
        "garbage": "not-a-jwt",
        "wrong_audience": keys.token(aud="https://another-server.example/mcp"),
        "api_audience": keys.token(aud="invoicedesk-api"),
        "no_audience": keys.token(aud=None),
        "wrong_resource_claim": keys.token(extra={"resource": "https://evil.example/mcp"}),
        "wrong_issuer": keys.token(iss="https://evil-issuer.example"),
        "expired": keys.token(ttl=-120),
        "unknown_key": keys.token(key=other, kid="attacker-key"),
        "known_kid_wrong_key": keys.token(key=other),
        "no_client": keys.token(client_id=None),
        "hs256_api_secret": jwt.encode(
            {
                "iss": ISSUER,
                "aud": RESOURCE,
                "client_id": "x",
                "scope": ALL,
                "exp": now + dt.timedelta(minutes=5),
            },
            "test-agent-jwt-secret-0123456789abcdef",
            algorithm="HS256",
        ),
        "alg_none": jwt.encode(
            {"iss": ISSUER, "aud": RESOURCE, "client_id": "x", "scope": ALL, "exp": 9999999999},
            key=None,  # pyright: ignore[reportArgumentType]
            algorithm="none",
        ),
    }
    async with harness(None, keys) as h:
        for label, token in bad_tokens.items():
            r = await h.rpc("tools/list", token=token)
            assert r.status_code == 401, label
            www = _www(r)
            assert www.startswith("Bearer ") and f'resource_metadata="{PRM}"' in www, label
            assert 'error="invalid_token"' in www, label
            assert "mcp-session-id" not in r.headers
        # nothing reached the API
        assert h.api.requests == []
        # a token for this resource is accepted, also when aud is a list naming it
        ok = await h.rpc("tools/list", token=keys.token(aud=["https://x.example", RESOURCE]))
        assert ok.status_code == 200, ok.text


async def test_tool_scopes_are_enforced_at_http_and_in_the_tool(keys: Keys) -> None:
    doc = "00000000-0000-4000-8000-000000000000"
    async with harness(None, keys) as h:
        read_only = keys.token("invoices:read")
        cases = [
            ("extract_invoice", {"file_url": "https://f.example/a.pdf"}, "invoices:write"),
            ("post_bill", {"document_id": doc}, "bills:post"),
        ]
        for name, args, needed in cases:
            r = await h.rpc(
                "tools/call", {"name": name, "arguments": args}, token=read_only, name=name
            )
            assert r.status_code == 403, name
            www = _www(r)
            assert 'error="insufficient_scope"' in www and f'scope="{needed}"' in www
            assert f'resource_metadata="{PRM}"' in www
        write_only = keys.token("invoices:write")
        r = await h.rpc(
            "tools/call",
            {"name": "validate_invoice", "arguments": {"document_id": doc}},
            token=write_only,
            name="validate_invoice",
        )
        assert r.status_code == 403 and 'scope="invoices:read"' in _www(r)

        # Lying in the routing header cannot smuggle a call past the gate: the SDK refuses a
        # Mcp-Name that differs from the body.
        r = await h.rpc(
            "tools/call",
            {"name": "extract_invoice", "arguments": {"file_url": "https://f.example/a.pdf"}},
            token=read_only,
            name="validate_invoice",
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == -32020  # HEADER_MISMATCH

        # tools/list needs only a valid token; it lists all three tools
        r = await h.rpc("tools/list", token=write_only)
        assert r.status_code == 200
        assert h.api.requests == []


async def test_scope_checked_inside_tool_without_routing_headers(keys: Keys) -> None:
    """A pre-2026 (stateless legacy) request carries no Mcp-Name; the tool still refuses."""
    async with harness(None, keys) as h, h.http() as c:
        r = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "extract_invoice",
                    "arguments": {"file_url": "https://f.example/a.pdf"},
                },
            },
            headers={
                "Authorization": f"Bearer {keys.token('invoices:read')}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200, r.text
        assert "mcp-session-id" not in r.headers
        body = r.json()
        assert body["result"]["isError"] is True
        assert "needs the invoices:write scope" in body["result"]["content"][0]["text"]
        assert h.api.requests == []


async def test_jwks_is_cached_and_refetch_is_rate_limited(keys: Keys) -> None:
    clock = [1000.0]
    keys.fetches.clear()
    src = JwksSource(url=JWKS_URL, http=jwks_http(keys), ttl_s=600, clock=lambda: clock[0])
    v = JwtTokenVerifier(jwks=src, issuer=ISSUER, resource_url=RESOURCE, algorithms=["RS256"])
    good = await v.verify_token(keys.token("invoices:read"))
    assert good is not None and good.scopes == ["invoices:read"] and good.resource == RESOURCE
    assert good.client_id.startswith("https://") and good.subject == "user_01JTEST"
    assert await v.verify_token(keys.token()) is not None
    assert len(keys.fetches) == 1  # cached
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for _ in range(5):
        assert await v.verify_token(keys.token(key=other, kid="rotated")) is None
    assert len(keys.fetches) == 1  # unknown kid within 30 s: no refetch storm
    clock[0] += 31
    assert await v.verify_token(keys.token(key=other, kid="rotated")) is None
    assert len(keys.fetches) == 2  # one refetch after the back-off
    clock[0] += 700
    assert await v.verify_token(keys.token()) is not None
    assert len(keys.fetches) == 3  # TTL expiry


async def test_scopes_from_scp_and_jwks_outage() -> None:
    from invoicedesk_mcp.testkit import make_keys

    k = make_keys()
    v = JwtTokenVerifier(
        jwks=JwksSource(url=JWKS_URL, http=jwks_http(k)),
        issuer=ISSUER,
        resource_url=RESOURCE,
        algorithms=["RS256"],
    )
    tok = await v.verify_token(k.token(extra={"scope": None, "scp": ["bills:post"]}))
    assert tok is not None and tok.scopes == ["bills:post"]

    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    v2 = JwtTokenVerifier(
        jwks=JwksSource(url=JWKS_URL, http=down),
        issuer=ISSUER,
        resource_url=RESOURCE,
        algorithms=["RS256"],
    )
    assert await v2.verify_token(k.token()) is None  # fail closed


async def test_jwks_outage_is_rate_limited_and_stale_keys_expire(keys: Keys) -> None:
    clock = [1000.0]
    up = [True]
    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=keys.jwks) if up[0] else httpx.Response(503)

    src = JwksSource(
        url=JWKS_URL,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ttl_s=600,
        min_refetch_s=30,
        max_stale_s=3600,
        clock=lambda: clock[0],
    )
    v = JwtTokenVerifier(jwks=src, issuer=ISSUER, resource_url=RESOURCE, algorithms=["RS256"])
    up[0] = False
    for _ in range(5):  # issuer down from the start: fail closed, one attempt per back-off
        assert await v.verify_token(keys.token()) is None
    assert len(calls) == 1
    clock[0] += 31
    up[0] = True
    assert await v.verify_token(keys.token()) is not None
    assert len(calls) == 2
    up[0] = False
    clock[0] += 700  # TTL expired and the issuer is down: stale keys still verify ...
    for _ in range(5):
        assert await v.verify_token(keys.token()) is not None
    assert len(calls) == 3  # ... but the dead issuer is not hammered on every request
    clock[0] += 3600  # past max staleness: fail closed
    assert await v.verify_token(keys.token()) is None


async def test_jwks_file_source_offline_only(tmp_path, keys: Keys) -> None:
    f = tmp_path / "jwks.json"
    f.write_text(json.dumps(keys.jwks))
    v = JwtTokenVerifier(
        jwks=JwksSource(file=f), issuer=ISSUER, resource_url=RESOURCE, algorithms=["RS256"]
    )
    assert await v.verify_token(keys.token()) is not None
    with pytest.raises(ConfigError):
        build_app(mcp_settings(MCP_AUTH_JWKS_FILE=str(f), INVOICEDESK_MODE="live"))


async def test_configuration_guards() -> None:
    with pytest.raises(ConfigError, match="MCP_AUTH_ISSUER"):
        build_app(mcp_settings(MCP_AUTH_ISSUER=None))
    with pytest.raises(ConfigError, match="AGENT_JWT_SECRET"):
        build_app(mcp_settings(AGENT_JWT_SECRET=None))
    with pytest.raises(ConfigError, match="https"):
        build_app(mcp_settings(INVOICEDESK_MODE="live", MCP_RESOURCE_URL="http://mcp.example/mcp"))
    s = mcp_settings(MCP_AUTH_ALGORITHMS="RS256 HS256 none ES256")
    assert s.algorithms == ["RS256", "ES256"]
    assert mcp_settings(MCP_AUTH_JWKS_URL=None).jwks_url == f"{ISSUER}/oauth2/jwks"
    with pytest.raises(ValueError, match="query"):
        mcp_settings(MCP_RESOURCE_URL="https://mcp.example/mcp?x=1")


async def test_devkeys_tokens_verify_against_the_jwks_file(tmp_path) -> None:
    from invoicedesk_mcp.devkeys import dev_token, ensure_dev_keys

    key, jwks_file = ensure_dev_keys(tmp_path / "dev")
    assert (tmp_path / "dev" / "private.pem").stat().st_mode & 0o077 == 0
    again, _ = ensure_dev_keys(tmp_path / "dev")  # reused, not regenerated
    assert again.private_numbers() == key.private_numbers()
    token = dev_token(key, issuer=ISSUER, resource=RESOURCE, scopes="invoices:read")
    v = JwtTokenVerifier(
        jwks=JwksSource(file=jwks_file), issuer=ISSUER, resource_url=RESOURCE, algorithms=["RS256"]
    )
    tok = await v.verify_token(token)
    assert tok is not None and tok.scopes == ["invoices:read"]
    assert tok.client_id == "invoicedesk-dev-client"


async def test_main_refuses_to_start_unconfigured(monkeypatch) -> None:
    from invoicedesk_mcp import server as srv

    monkeypatch.setattr(srv, "get_mcp_settings", lambda: mcp_settings(MCP_AUTH_ISSUER=None))
    with pytest.raises(SystemExit, match="MCP_AUTH_ISSUER"):
        srv.main([])
