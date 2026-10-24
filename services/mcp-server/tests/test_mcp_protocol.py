"""Protocol surface: stateless 2026-07-28 serving, tools/list ttlMs hints, raw MRTR round-trip."""

from __future__ import annotations

from typing import Any

import jwt
import pytest
from invoicedesk.money import format_cents
from invoicedesk_api.testkit import AGENT_SECRET, demo_file
from invoicedesk_mcp.auth import TOOL_SCOPES
from invoicedesk_mcp.testkit import CLIENT_ID, Keys, accept, harness
from invoicedesk_mcp.tools import DESCRIPTIONS

pytestmark = pytest.mark.asyncio


async def test_tools_list_is_small_cacheable_and_sessionless(keys: Keys) -> None:
    async with harness(None, keys, MCP_TOOLS_LIST_TTL_MS=120_000) as h:
        r = await h.rpc("tools/list", token=keys.token())
        assert r.status_code == 200
        assert "mcp-session-id" not in r.headers  # stateless: no session, ever
        result = r.json()["result"]
        assert result["ttlMs"] == 120_000 and result["cacheScope"] == "public"
        tools = {t["name"]: t for t in result["tools"]}
        assert (
            set(tools) == {"extract_invoice", "validate_invoice", "post_bill"} == set(TOOL_SCOPES)
        )
        for name, tool in tools.items():
            words = len(tool["description"].split())
            assert words < 60, (name, words)
            assert tool["description"] == DESCRIPTIONS[name]
            assert "outputSchema" in tool
        assert tools["validate_invoice"]["annotations"]["readOnlyHint"] is True
        assert tools["post_bill"]["annotations"]["idempotentHint"] is True
        assert tools["post_bill"]["annotations"]["destructiveHint"] is False
        # post_bill takes only the document handle: no way for the model to pass a confirmation
        assert set(tools["post_bill"]["inputSchema"]["properties"]) == {"document_id"}
        assert set(tools["extract_invoice"]["inputSchema"]["properties"]) == {
            "file_url",
            "upload_handle",
        }

        # The SDK client honours the hint (second list served from its cache).
        async with h.client(keys.token()) as c:
            first = await c.list_tools()
            second = await c.list_tools()
        assert [t.name for t in first.tools] == [t.name for t in second.tools]


def _content(result: dict[str, Any]) -> str:
    return " ".join(c.get("text", "") for c in result.get("content", []))


@pytest.mark.postgres
async def test_post_bill_mrtr_input_required_round_trip(env, keys: Keys) -> None:
    """The confirmation is an MRTR ``input_required`` result with sealed ``requestState``; the
    retry carries ``inputResponses``. A forged or foreign ``requestState`` is rejected."""
    f = demo_file("04-voltwise")
    up = env.client.post(
        "/api/v1/agent/uploads",
        files={"file": (f.filename, f.data, f.mime)},
        headers=env.agent("invoices:write"),
    ).json()["upload_handle"]
    async with harness(env, keys) as h:
        token = keys.token()
        ex = await h.rpc(
            "tools/call",
            {"name": "extract_invoice", "arguments": {"upload_handle": up}},
            token=token,
            name="extract_invoice",
        )
        out = ex.json()["result"]["structuredContent"]
        doc_id, vendor, total = (
            out["document_id"],
            out["fields"]["vendor_name"],
            int(out["fields"]["total"]),
        )
        call = {"name": "post_bill", "arguments": {"document_id": doc_id}}

        first = await h.rpc("tools/call", call, token=token, name="post_bill")
        assert first.status_code == 200 and "mcp-session-id" not in first.headers
        res = first.json()["result"]
        assert res["resultType"] == "input_required"
        (key, req), *rest = res["inputRequests"].items()
        assert rest == []
        assert req["method"] == "elicitation/create"
        assert req["params"].get("mode", "form") == "form"
        assert set(req["params"]["requestedSchema"]["properties"]) == {
            "vendor_name",
            "total",
            "approve",
        }
        assert format_cents(total) in req["params"]["message"]
        state = res["requestState"]
        assert isinstance(state, str) and doc_id not in state  # sealed, not readable
        assert env.accounting.bills == {}  # nothing posted before the human answered

        answer = {
            key: {
                "action": "accept",
                "content": {"vendor_name": vendor, "total": format_cents(total), "approve": True},
            }
        }
        # A tampered requestState is refused, and nothing is posted.
        forged = await h.rpc(
            "tools/call",
            {**call, "inputResponses": answer, "requestState": state[:-4] + "AAAA"},
            token=token,
            name="post_bill",
        )
        assert "error" in forged.json() or forged.json()["result"].get("isError")
        assert env.accounting.bills == {}

        # State is bound to the principal: another user of the same client cannot replay it.
        other_user = keys.token(sub="user_02OTHER")
        stolen = await h.rpc(
            "tools/call",
            {**call, "inputResponses": answer, "requestState": state},
            token=other_user,
            name="post_bill",
        )
        assert "error" in stolen.json() or stolen.json()["result"].get("isError")
        assert env.accounting.bills == {}

        second = await h.rpc(
            "tools/call",
            {**call, "inputResponses": answer, "requestState": state},
            token=token,
            name="post_bill",
        )
        done = second.json()["result"]
        assert done.get("resultType", "complete") == "complete", done
        assert done["structuredContent"]["status"] == "Posted", _content(done)
        assert len(env.accounting.bills) == 1

        # Service tokens minted for the facade: HS256, only the tool's scope, act = client.
        seen: dict[str, set[str]] = {}
        for req_rec in h.api.requests:
            auth = req_rec["headers"]["authorization"].removeprefix("Bearer ")
            claims = jwt.decode(
                auth, AGENT_SECRET, algorithms=["HS256"], audience="invoicedesk-api"
            )
            assert claims["act"] == {"sub": CLIENT_ID} and claims["sub"] == "user_01JTEST"
            assert claims["exp"] - claims["iat"] <= 120
            seen.setdefault(req_rec["path"].rsplit("/", 1)[-1], set()).add(claims["scope"])
        assert seen == {
            "extract": {"invoices:write"},
            "validation": {"invoices:read"},
            "post-bill": {"bills:post"},
        }


@pytest.mark.postgres
async def test_legacy_stateless_client_cannot_be_asked_so_post_bill_refuses(
    env, keys: Keys
) -> None:
    """A pre-2026 client on this stateless server has no back-channel for elicitation, so
    post_bill cannot obtain a human confirmation and must refuse (never post unconfirmed)."""
    f = demo_file("04-voltwise")
    up = env.client.post(
        "/api/v1/agent/uploads",
        files={"file": (f.filename, f.data, f.mime)},
        headers=env.agent("invoices:write"),
    ).json()["upload_handle"]
    async with harness(env, keys) as h:
        token = keys.token()
        async with h.client(token, mode="legacy", elicit=accept("x", "1.00")) as c:
            tools = await c.list_tools()
            assert {t.name for t in tools.tools} == set(TOOL_SCOPES)
            out = await c.call_tool("extract_invoice", {"upload_handle": up})
            assert not out.is_error and out.structured_content is not None
            doc_id = out.structured_content["document_id"]
            res = await c.call_tool("post_bill", {"document_id": doc_id})
        assert res.is_error
        assert "2026-07-28" in " ".join(getattr(x, "text", "") for x in res.content)
        assert env.status(doc_id) == "ReadyToPay" and env.accounting.bills == {}
        assert all(not r["path"].endswith("/post-bill") for r in h.api.requests)
