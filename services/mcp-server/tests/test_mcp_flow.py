"""End to end over MCP (2026-07-28, stateless): extract -> validate -> post_bill (M16, US-13)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from invoicedesk.money import format_cents
from invoicedesk_api.testkit import DEMO_ACCOUNT_NUMBERS, demo_file, unmasked_numbers
from invoicedesk_mcp.testkit import CLIENT_ID, Harness, accept, decline, harness

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _upload_handle(h: Harness, prefix: str) -> str:
    f = demo_file(prefix)
    r = h.env.client.post(
        "/api/v1/agent/uploads",
        files={"file": (f.filename, f.data, f.mime)},
        headers=h.env.agent("invoices:write"),
    )
    assert r.status_code == 201, r.text
    return r.json()["upload_handle"]


def _structured(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def _error_text(result: Any) -> str:
    assert result.is_error, result.structured_content
    return " ".join(getattr(c, "text", "") for c in result.content)


def _ocr_passages(h: Harness, doc_id: str, run: int = 6) -> list[str]:
    """Every run of ``run`` consecutive OCR words, plus every text-layer span and every
    screening span: the document text the MCP server must never receive."""
    rows = h.env.sql(
        "select output_json from pipeline_steps where document_id = %s and step = 'preprocess'",
        (doc_id,),
    )
    passages: list[str] = []
    if rows:
        doc = rows[0]["output_json"]["doc"]
        words = [w["text"] for w in doc.get("words", [])]
        passages += [" ".join(words[i : i + run]) for i in range(max(0, len(words) - run + 1))]
        passages += [s["text"] for s in doc.get("text_layer", []) if len(s["text"]) >= 12]
    for r in h.env.sql("select spans_json from screen_results where document_id = %s", (doc_id,)):
        passages += [s.get("text", "") for s in (r["spans_json"] or []) if s.get("text")]
    return [p for p in passages if p.strip()]


def _assert_no_raw_text(h: Harness, doc_id: str, *extra: Any) -> None:
    received = json.dumps([h.api.responses, *extra])
    # Extracted field *values* are what the facade is meant to return; a passage that is just
    # (part of) one of them, such as a city in the vendor address, is not leaked document text.
    values = [r["value"] for r in h.env.sql("select value from fields where value is not null")]
    for passage in _ocr_passages(h, doc_id):
        if any(passage in v for v in values):
            continue
        assert passage not in received, f"raw document text reached the MCP server: {passage!r}"
    assert _account_like(h.api.responses) == []
    assert not any(n in received for n in DEMO_ACCOUNT_NUMBERS)


_MONEY = {"subtotal", "tax", "total"}


def _account_like(obj: Any, key: str | None = None) -> list[str]:
    """Strings with a 6+ digit run, except integer-cents money values (by contract)."""
    if isinstance(obj, dict):
        found: list[str] = []
        for k, v in obj.items():
            if k in _MONEY and isinstance(v, dict | str):
                continue  # {"total": {"value": "124000"}} or {"total": "124000"}
            found += _account_like(v, k)
        return found
    if isinstance(obj, list):
        return [x for v in obj for x in _account_like(v, key)]
    return unmasked_numbers(obj) if isinstance(obj, str) else []


async def test_clean_invoice_extract_validate_post(env, keys) -> None:
    async with harness(env, keys) as h:
        handle = _upload_handle(h, "04-voltwise")
        token = keys.token()
        async with h.client(token) as c:
            out = _structured(await c.call_tool("extract_invoice", {"upload_handle": handle}))
        assert set(out) == {
            "document_id",
            "status",
            "fields",
            "calibrated_confidence",
            "flags",
            "embedded_instruction_count",
        }
        assert out["status"] == "ReadyToPay" and out["flags"] == []
        assert set(out["fields"]) == set(out["calibrated_confidence"])
        assert all(0 <= p <= 1 for p in out["calibrated_confidence"].values() if p is not None)
        doc_id = out["document_id"]
        vendor = out["fields"]["vendor_name"]
        total = int(out["fields"]["total"])

        async with h.client(token) as c:
            v = _structured(await c.call_tool("validate_invoice", {"document_id": doc_id}))
        assert v["status"] == "ReadyToPay" and len(v["rule_results"]) == 10
        assert all(r["passed"] for r in v["rule_results"]) and v["variance_explanations"] == []
        assert all("details" not in r for r in v["rule_results"])

        # The human types what is printed on the invoice; the API checks it.
        cb = accept(vendor.upper(), format_cents(total))
        async with h.client(token, elicit=cb) as c:
            bill = _structured(await c.call_tool("post_bill", {"document_id": doc_id}))
        assert bill["status"] == "Posted" and bill["system"] == "fake" and bill["bill_id"]
        assert len(cb.seen) == 1  # type: ignore[attr-defined]
        prompt = cb.seen[0]  # type: ignore[attr-defined]
        assert doc_id in prompt.message and format_cents(total) in prompt.message
        assert set(prompt.requested_schema["properties"]) == {"vendor_name", "total", "approve"}
        assert env.status(doc_id) == "Posted" and len(env.accounting.bills) == 1

        # Idempotent: a retry (new confirmation round) returns the same bill, nothing re-posted.
        async with h.client(token, elicit=accept(vendor, f"{total / 100:.2f}")) as c:
            again = _structured(await c.call_tool("post_bill", {"document_id": doc_id}))
        assert again == bill and len(env.accounting.bills) == 1

        # Audit actor is the OAuth client: mcp:<client_id>
        actors = {
            r["action"]: r["actor"]
            for r in env.sql(
                "select action, actor from audit_log where document_id = %s"
                " and action in ('document.created', 'status.changed')"
                " and (action = 'document.created' or after_json->>'status' = 'Posted')",
                (doc_id,),
            )
        }
        assert actors == {
            "document.created": f"mcp:{CLIENT_ID}",
            "status.changed": f"mcp:{CLIENT_ID}",
        }
        row = env.sql("select source::text as s from documents where id = %s", (doc_id,))[0]
        assert row["s"] == "mcp"

        _assert_no_raw_text(h, doc_id, out, v, bill)
        # The MCP server never sent document bytes or text to the API either.
        assert all(r["path"] != "/api/v1/agent/uploads" for r in h.api.requests)


async def test_post_bill_refuses_flagged_bank_change_from_email(env, keys) -> None:
    """R6 (bank-detail change in the e-mail body) -> Flagged: refused before any confirmation."""
    import base64
    import hashlib
    import hmac

    from invoicedesk_api.testkit import INGEST_SECRET
    from invoicedesk_data.demo import bank_change_email

    mail = bank_change_email()
    att = mail.attachments[0]
    raw = json.dumps(
        {
            "message_id": mail.message_id,
            "from": mail.from_addr,
            "subject": mail.subject,
            "body_text": mail.body_text,
            "received_at": "2026-10-20T09:15:00Z",
            "attachments": [
                {
                    "filename": att.filename,
                    "content_type": att.mime,
                    "content_b64": base64.b64encode(att.data).decode(),
                }
            ],
        }
    ).encode()
    sig = hmac.new(INGEST_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    r = env.client.post(
        "/api/v1/ingest/email",
        content=raw,
        headers={"X-InvoiceDesk-Signature": f"sha256={sig}", "Content-Type": "application/json"},
    )
    assert r.status_code == 202, r.text
    doc_id = r.json()["documents"][0]["id"]
    env.drain()
    assert env.status(doc_id) == "Flagged"

    async with harness(env, keys) as h:
        token = keys.token()
        async with h.client(token) as c:
            v = _structured(await c.call_tool("validate_invoice", {"document_id": doc_id}))
        assert v["status"] == "Flagged"
        r6 = next(x for x in v["rule_results"] if x["rule_id"] == "R6")
        assert not r6["passed"] and r6["severity"] == "flag"

        cb = accept("Copperleaf", "1.00")
        async with h.client(token, elicit=cb) as c:
            res = await c.call_tool("post_bill", {"document_id": doc_id})
        text = _error_text(res)
        assert "Flagged" in text and "two human approvers" in text
        assert cb.seen == []  # type: ignore[attr-defined]  # never asked the human
        assert env.status(doc_id) == "Flagged" and env.accounting.bills == {}
        assert all("post-bill" not in r["path"] for r in h.api.requests)
        # The bank account in the e-mail never reaches the MCP server unmasked.
        assert "000555077001" not in json.dumps(h.api.responses)
        _assert_no_raw_text(h, doc_id, v)


async def test_post_bill_refuses_flagged_hidden_text_and_not_ready(env, keys) -> None:
    async with harness(env, keys) as h:
        token = keys.token()
        ember = _upload_handle(h, "18-ember")
        rooter = _upload_handle(h, "10-rapid-rooter")
        async with h.client(token) as c:
            flagged = _structured(await c.call_tool("extract_invoice", {"upload_handle": ember}))
            review = _structured(await c.call_tool("extract_invoice", {"upload_handle": rooter}))
        assert flagged["status"] == "Flagged" and "hidden_text" in flagged["flags"]
        assert flagged["embedded_instruction_count"] >= 0
        assert review["status"] == "NeedsReview"

        cb = accept("Ember Hearth", "1.00")
        async with h.client(token, elicit=cb) as c:
            res_f = await c.call_tool("post_bill", {"document_id": flagged["document_id"]})
            res_r = await c.call_tool("post_bill", {"document_id": review["document_id"]})
            v = _structured(
                await c.call_tool("validate_invoice", {"document_id": review["document_id"]})
            )
        assert "Flagged" in _error_text(res_f)
        text_r = _error_text(res_r)
        assert "only ReadyToPay" in text_r and "NeedsReview" in text_r
        assert cb.seen == []  # type: ignore[attr-defined]
        assert env.accounting.bills == {}
        assert v["variance_explanations"] and "$340.00 over" in v["variance_explanations"][0]
        _assert_no_raw_text(h, flagged["document_id"], flagged)
        _assert_no_raw_text(h, review["document_id"], review, v)
        assert "AI assistant" not in json.dumps([h.api.responses, flagged])


async def test_confirmation_mismatch_decline_and_unticked_are_refused(env, keys) -> None:
    async with harness(env, keys) as h:
        token = keys.token()
        handle = _upload_handle(h, "04-voltwise")
        async with h.client(token) as c:
            out = _structured(await c.call_tool("extract_invoice", {"upload_handle": handle}))
        doc_id, vendor, total = (
            out["document_id"],
            out["fields"]["vendor_name"],
            int(out["fields"]["total"]),
        )

        cases = [
            (accept(vendor, format_cents(total + 1)), "does not match"),
            (accept("Some Other Vendor LLC", format_cents(total)), "does not match"),
            (accept(vendor, format_cents(total), approve=False), "did not tick"),
            (accept(vendor, "twelve dollars"), "not an amount"),
            (decline(), "declined"),
        ]
        for cb, expected in cases:
            async with h.client(token, elicit=cb) as c:
                res = await c.call_tool("post_bill", {"document_id": doc_id})
            assert expected in _error_text(res), expected
        assert env.status(doc_id) == "ReadyToPay" and env.accounting.bills == {}
        posts = [r for r in h.api.requests if r["path"].endswith("/post-bill")]
        assert len(posts) == 2  # only the two typed mismatches reached the API (and were 422)
        assert all(r["json"]["confirmation"]["confirmed_by_human"] is True for r in posts)


async def test_bad_arguments_are_tool_errors(env, keys) -> None:
    # SSE response mode (the production default) for this one.
    async with harness(env, keys, MCP_JSON_RESPONSE=False) as h:
        token = keys.token()
        async with h.client(token) as c:
            both = await c.call_tool(
                "extract_invoice", {"file_url": "https://x.example/a.pdf", "upload_handle": "x"}
            )
            neither = await c.call_tool("extract_invoice", {})
            ftp = await c.call_tool("extract_invoice", {"file_url": "ftp://x.example/a.pdf"})
            foreign = await c.call_tool(
                "extract_invoice", {"upload_handle": "maple_row/originals/x/y.pdf"}
            )
            bad_id = await c.call_tool("validate_invoice", {"document_id": "../../" + "a" * 30})
            unknown = await c.call_tool(
                "validate_invoice", {"document_id": "00000000-0000-4000-8000-000000000000"}
            )
        assert "exactly one" in _error_text(both) and "exactly one" in _error_text(neither)
        assert "https" in _error_text(ftp)
        assert "No such document" in _error_text(foreign)
        assert "UUID" in _error_text(bad_id)
        assert "No such document" in _error_text(unknown)
