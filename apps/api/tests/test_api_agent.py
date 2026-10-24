"""Agent facade for the MCP server (M16, US-13): scopes, no raw text, post-bill guards."""

from __future__ import annotations

import json

import pytest
from invoicedesk_api.fetch import OfflineFetcher
from invoicedesk_api.testkit import CLERK, demo_file, unmasked_numbers

pytestmark = pytest.mark.postgres

WRITE = "invoices:write"
READ = "invoices:read"
POST = "bills:post"


def _handle(env, prefix: str) -> str:
    f = demo_file(prefix)
    r = env.client.post(
        "/api/v1/agent/uploads",
        files={"file": (f.filename, f.data, f.mime)},
        headers=env.agent(WRITE),
    )
    assert r.status_code == 201, r.text
    return r.json()["upload_handle"]


def test_scopes_and_tokens(env) -> None:
    f = demo_file("04-voltwise")
    files = {"file": (f.filename, f.data, f.mime)}
    assert env.client.post("/api/v1/agent/uploads", files=files).status_code == 401
    r = env.client.post("/api/v1/agent/uploads", files=files, headers=env.agent(READ))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert (
        env.client.post(
            "/api/v1/agent/uploads", files=files, headers=env.agent(WRITE, ttl=-5)
        ).status_code
        == 401
    )
    # a staff (Supabase) token is not a service token
    assert (
        env.client.post("/api/v1/agent/uploads", files=files, headers=env.bearer(CLERK)).status_code
        == 401
    )


def test_extract_validate_post_flow(env) -> None:
    handle = _handle(env, "04-voltwise")
    assert handle.startswith("maple_row/agent-uploads/")
    r = env.client.post(
        "/api/v1/agent/extract", json={"upload_handle": handle}, headers=env.agent(WRITE)
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"document_id", "status", "fields", "flags", "embedded_instructions"}
    assert out["status"] == "ReadyToPay"
    assert set(out["fields"]["total"]) == {"value", "calibrated_confidence"}
    assert "remit_to_bank_details" not in out["fields"] and "vendor_id" not in out["fields"]
    doc_id = out["document_id"]
    row = env.sql("select source::text as s from documents where id = %s", (doc_id,))[0]
    assert row["s"] == "mcp"
    audit = env.sql(
        "select actor from audit_log where document_id = %s and action = 'document.created'",
        (doc_id,),
    )
    assert audit[0]["actor"] == "mcp:claude-desktop"

    v = env.client.get(
        f"/api/v1/agent/documents/{doc_id}/validation", headers=env.agent(READ)
    ).json()
    assert (
        v["status"] == "ReadyToPay"
        and len(v["rule_results"]) == 10
        and v["variance_explanations"] == []
    )
    assert (
        env.client.get(
            f"/api/v1/agent/documents/{doc_id}/validation", headers=env.agent(WRITE)
        ).status_code
        == 403
    )

    url = f"/api/v1/agent/documents/{doc_id}/post-bill"
    vendor = out["fields"]["vendor_name"]["value"]
    total = int(out["fields"]["total"]["value"])
    no_human = env.client.post(
        url,
        json={
            "confirmation": {
                "vendor_name": vendor,
                "total_cents": total,
                "confirmed_by_human": False,
            }
        },
        headers=env.agent(POST),
    )
    assert (
        no_human.status_code == 422 and no_human.json()["error"]["code"] == "confirmation_required"
    )
    mismatch = env.client.post(
        url,
        json={
            "confirmation": {
                "vendor_name": vendor,
                "total_cents": total + 1,
                "confirmed_by_human": True,
            }
        },
        headers=env.agent(POST),
    )
    assert (
        mismatch.status_code == 422 and mismatch.json()["error"]["code"] == "confirmation_mismatch"
    )
    floaty = env.client.post(
        url,
        json={
            "confirmation": {
                "vendor_name": vendor,
                "total_cents": total + 0.5,
                "confirmed_by_human": True,
            }
        },
        headers=env.agent(POST),
    )
    assert floaty.status_code == 422  # money is integer cents, never a float
    good = {
        "confirmation": {
            "vendor_name": vendor.upper(),
            "total_cents": total,
            "confirmed_by_human": True,
        }
    }
    assert env.client.post(url, json=good, headers=env.agent(WRITE)).status_code == 403
    posted = env.client.post(url, json=good, headers=env.agent(POST))
    assert posted.status_code == 200, posted.text
    bill = posted.json()
    assert bill["status"] == "Posted" and bill["system"] == "fake" and bill["bill_id"]
    again = env.client.post(url, json=good, headers=env.agent(POST)).json()
    assert again == bill  # idempotent
    assert len(env.accounting.bills) == 1
    actor = env.sql(
        "select actor from audit_log where document_id = %s and action = 'status.changed'"
        " and after_json->>'status' = 'Posted'",
        (doc_id,),
    )
    assert actor[0]["actor"] == "mcp:claude-desktop"


def test_flagged_document_is_never_posted_by_an_agent(env) -> None:
    handle = _handle(env, "18-ember")
    out = env.client.post(
        "/api/v1/agent/extract", json={"upload_handle": handle}, headers=env.agent(WRITE)
    ).json()
    assert out["status"] == "Flagged" and "hidden_text" in out["flags"]
    # no raw document text: no OCR words, spans or the hidden instruction itself
    text = json.dumps(out)
    assert (
        "AI assistant" not in text and "routing" not in text.lower() and unmasked_numbers(out) == []
    )
    r = env.client.post(
        f"/api/v1/agent/documents/{out['document_id']}/post-bill",
        json={"confirmation": {"vendor_name": "x", "total_cents": 1, "confirmed_by_human": True}},
        headers=env.agent(POST),
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "illegal_transition"


def test_over_estimate_variance_explanation(env) -> None:
    handle = _handle(env, "10-rapid-rooter")
    out = env.client.post(
        "/api/v1/agent/extract", json={"upload_handle": handle}, headers=env.agent(WRITE)
    ).json()
    assert out["status"] == "NeedsReview"
    v = env.client.get(
        f"/api/v1/agent/documents/{out['document_id']}/validation", headers=env.agent(READ)
    ).json()
    assert v["variance_explanations"] and "$340.00 over" in v["variance_explanations"][0]
    r = env.client.post(
        f"/api/v1/agent/documents/{out['document_id']}/post-bill",
        json={
            "confirmation": {
                "vendor_name": "Rapid Rooter Plumbing LLC",
                "total_cents": 1,
                "confirmed_by_human": True,
            }
        },
        headers=env.agent(POST),
    )
    assert r.status_code == 409


def test_extract_input_validation(env_factory) -> None:
    f = demo_file("04-voltwise")
    env = env_factory(
        fetcher=OfflineFetcher({"https://files.example/inv.pdf": (f.data, "application/pdf")})
    )
    h = env.agent(WRITE)
    both = env.client.post(
        "/api/v1/agent/extract",
        json={"file_url": "https://x.example/a.pdf", "upload_handle": "x"},
        headers=h,
    )
    assert both.status_code == 422
    assert env.client.post("/api/v1/agent/extract", json={}, headers=h).status_code == 422
    foreign = env.client.post(
        "/api/v1/agent/extract", json={"upload_handle": "maple_row/originals/x/y.pdf"}, headers=h
    )
    assert foreign.status_code == 404
    offline = env.client.post(
        "/api/v1/agent/extract", json={"file_url": "https://elsewhere.example/a.pdf"}, headers=h
    )
    assert offline.status_code == 422 and offline.json()["error"]["code"] == "rejected_input"
    ok = env.client.post(
        "/api/v1/agent/extract", json={"file_url": "https://files.example/inv.pdf"}, headers=h
    )
    assert ok.status_code == 200 and ok.json()["status"] == "ReadyToPay"
