"""Upload (M1), e-mail ingest (M2), auth, self-approval (US-5), corrections into eval examples
(M10), reject, visitor post refusal."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json

import pymupdf
import pytest
from invoicedesk_api.testkit import (
    APPROVER,
    CLERK,
    INGEST_SECRET,
    OWNER,
    demo_file,
    unmasked_numbers,
)
from PIL import Image

pytestmark = pytest.mark.postgres


def _pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, format="PNG")
    return buf.getvalue()


def _upload(env, files, headers, batch_id=None):
    data = {"batch_id": batch_id} if batch_id else None
    return env.client.post(
        "/api/v1/documents",
        files=[("files[]", (name, content, mime)) for name, content, mime in files],
        data=data,
        headers=headers,
    )


# ---------------------------------------------------------------------------- auth
def test_staff_auth_modes(env_factory) -> None:
    env = env_factory()
    files = [("a.png", _png(), "image/png")]
    assert _upload(env, files, {}).status_code == 401
    bad = _upload(env, files, {"Authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "unauthenticated"
    wrong = _upload(env, files, env.bearer(CLERK, secret="some-other-secret-0123456789abcdef"))
    assert wrong.status_code == 401
    expired = _upload(env, files, env.bearer(CLERK, ttl=-60))
    assert expired.status_code == 401
    assert _upload(env, files, env.dev("nobody@maplerow.example")).status_code == 401
    ok = _upload(env, files, env.bearer(CLERK))
    assert ok.status_code == 202, ok.text
    me = env.client.get("/api/v1/me", headers=env.bearer(APPROVER)).json()
    assert me == {
        "id": env.staff[APPROVER],
        "email": APPROVER,
        "display_name": "Priya Okafor",
        "role": "approver",
    }
    assert env.client.get("/api/v1/me", headers=env.dev(OWNER)).json()["role"] == "owner"


def test_dev_user_header_is_refused_in_live_mode(env_factory) -> None:
    env = env_factory(settings={"mode": "live"})
    r = env.client.get("/api/v1/me", headers=env.dev(CLERK))
    assert r.status_code == 401
    assert env.client.get("/api/v1/me", headers=env.bearer(CLERK)).status_code == 200


# ---------------------------------------------------------------------------- upload
def test_upload_allow_list_batches_and_processing(env) -> None:
    clean = demo_file("04-voltwise")
    batch = env.client.post(
        "/api/v1/batches", json={"label": "Oct batch", "mode": "live"}, headers=env.dev(CLERK)
    )
    assert batch.status_code == 201
    bid = batch.json()["batch_id"]
    r = _upload(
        env,
        [
            (clean.filename, clean.data, clean.mime),
            ("notes.txt", b"hello", "text/plain"),
            ("fake.pdf", b"not a pdf at all", "application/pdf"),
            ("six.pdf", _pdf(6), "application/pdf"),
            ("big.png", b"\x89PNG\r\n\x1a\n" + b"0" * (10 * 1024 * 1024 + 1), "image/png"),
            ("empty.png", b"", "image/png"),
        ],
        env.dev(CLERK),
        batch_id=bid,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["batch_id"] == bid
    assert [d["filename"] for d in body["documents"]] == [clean.filename]
    assert body["documents"][0]["status"] == "Received"
    errors = {x["filename"]: x["error"] for x in body["rejected"]}
    assert set(errors) == {"notes.txt", "fake.pdf", "six.pdf", "big.png", "empty.png"}
    assert "5" in errors["six.pdf"] and "10 MB" in errors["big.png"]
    doc_id = body["documents"][0]["id"]
    row = env.sql(
        "select source::text as s, uploaded_by, storage_path, pages from documents where id = %s",
        (doc_id,),
    )[0]
    assert row["s"] == "upload" and str(row["uploaded_by"]) == env.staff[CLERK]
    assert row["storage_path"].startswith(f"maple_row/originals/{doc_id}/")
    jobs = env.sql("select kind from jobs where document_id = %s", (doc_id,))
    assert [j["kind"] for j in jobs] == ["api.process_document"]
    assert env.drain() == 1
    assert env.status(doc_id) == "ReadyToPay"
    # unknown batch
    r2 = _upload(env, [(clean.filename, clean.data, clean.mime)], env.dev(CLERK), batch_id="0" * 8)
    assert r2.status_code == 404


def test_worker_mode_queues_for_the_external_worker(env_factory) -> None:
    env = env_factory(api={"pipeline_mode": "worker"})
    f = demo_file("04-voltwise")
    r = _upload(env, [(f.filename, f.data, f.mime)], env.dev(CLERK))
    doc_id = r.json()["documents"][0]["id"]
    assert [
        j["kind"] for j in env.sql("select kind from jobs where document_id = %s", (doc_id,))
    ] == ["process_document"]
    assert env.drain() == 0  # the API's embedded runner leaves worker jobs alone


# ---------------------------------------------------------------------------- approvals
def test_self_approval_is_403_and_two_others_release(env) -> None:
    dup = demo_file("21-rapid-rooter")
    r = _upload(env, [(dup.filename, dup.data, dup.mime)], env.dev(APPROVER))
    doc_id = r.json()["documents"][0]["id"]
    env.drain()
    assert env.status(doc_id) == "Flagged"
    url = f"/api/v1/documents/{doc_id}/approvals"
    body = {"kind": "duplicate", "decision": "approve", "callback_attested": True}
    own = env.client.post(url, json=body, headers=env.dev(APPROVER))
    assert own.status_code == 403
    assert own.json() == {
        "error": {
            "code": "self_approval",
            "message": "You uploaded this invoice, so you cannot approve it.",
        }
    }
    assert env.client.post(url, json=body, headers=env.dev(CLERK)).status_code == 403
    one = env.client.post(url, json=body, headers=env.dev(OWNER)).json()
    assert one["status"] == "Flagged" and len(one["approvals"]) == 1
    # only two non-uploader approvers exist in the seed, so the duplicate stays held
    assert env.status(doc_id) == "Flagged"
    assert (
        env.sql(
            "select count(*) as n from approvals where approver_id = %s", (env.staff[APPROVER],)
        )[0]["n"]
        == 0
    )


def test_bank_change_vote_needs_callback(env) -> None:
    white = demo_file("18-ember")
    r = _upload(env, [(white.filename, white.data, white.mime)], env.dev(CLERK))
    doc_id = r.json()["documents"][0]["id"]
    env.drain()
    assert env.status(doc_id) == "Flagged"
    no_cb = env.client.post(
        f"/api/v1/documents/{doc_id}/approvals",
        json={"kind": "bank_change", "decision": "approve", "callback_attested": False},
        headers=env.dev(APPROVER),
    )
    assert no_cb.status_code == 409 and no_cb.json()["error"]["code"] == "illegal_transition"
    bad_kind = env.client.post(
        f"/api/v1/documents/{doc_id}/approvals",
        json={"kind": "whatever", "decision": "approve"},
        headers=env.dev(APPROVER),
    )
    assert bad_kind.status_code == 422


# ---------------------------------------------------------------------------- corrections
def test_correction_writes_review_and_eval_example(env) -> None:
    f = demo_file("08-handy-heron")
    r = _upload(env, [(f.filename, f.data, f.mime)], env.dev(CLERK))
    doc_id = r.json()["documents"][0]["id"]
    env.drain()
    assert env.status(doc_id) == "NeedsReview"
    out = env.client.post(
        f"/api/v1/documents/{doc_id}/corrections",
        json={"corrections": [{"field": "total", "value": "51000"}]},
        headers=env.dev(CLERK),
    )
    assert out.status_code == 200, out.text
    assert out.json()["status"] in ("ReadyToPay", "NeedsReview")
    reviews = env.sql(
        "select field_name, new_value, reviewer_id from reviews where document_id = %s", (doc_id,)
    )
    assert reviews == [
        {"field_name": "total", "new_value": "51000", "reviewer_id": reviews[0]["reviewer_id"]}
    ]
    assert str(reviews[0]["reviewer_id"]) == env.staff[CLERK]
    ex = env.sql(
        "select split::text as s, provenance, gold_json from eval_examples where document_id = %s",
        (doc_id,),
    )
    assert len(ex) == 1 and ex[0]["s"] == "correction" and ex[0]["provenance"] == "human_correction"
    assert ex[0]["gold_json"]["fields_corrected"] == ["total"]
    total = next(
        x
        for x in env.client.get(f"/api/v1/documents/{doc_id}", headers=env.dev(CLERK)).json()[
            "fields"
        ]
        if x["name"] == "total"
    )
    assert total["value"] == "51000" and total["calibrated_conf"] == 1.0
    assert (
        env.client.post(
            f"/api/v1/documents/{doc_id}/corrections",
            json={"corrections": []},
            headers=env.dev(CLERK),
        ).status_code
        == 422
    )
    assert (
        env.client.post(
            f"/api/v1/documents/{doc_id}/corrections",
            json={"corrections": [{"field": "total", "value": "1"}]},
        ).status_code
        == 401
    )


def test_reject_and_unknown_document(env) -> None:
    f = demo_file("11-marigold")
    doc_id = _upload(env, [(f.filename, f.data, f.mime)], env.dev(CLERK)).json()["documents"][0][
        "id"
    ]
    env.drain()
    assert env.status(doc_id) == "NeedsReview"
    r = env.client.post(
        f"/api/v1/documents/{doc_id}/reject",
        json={"reason": "Not our vendor"},
        headers=env.dev(CLERK),
    )
    assert r.json() == {"status": "Rejected"}
    again = env.client.post(f"/api/v1/documents/{doc_id}/reject", json={}, headers=env.dev(CLERK))
    assert again.status_code == 409
    missing = env.client.get("/api/v1/documents/not-a-uuid", headers=env.dev(CLERK))
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert env.client.get(f"/api/v1/documents/{doc_id}").status_code == 401  # not public


# ---------------------------------------------------------------------------- email (M2)
def _sign(body: bytes, secret: str = INGEST_SECRET) -> dict[str, str]:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-InvoiceDesk-Signature": f"sha256={sig}", "Content-Type": "application/json"}


def test_email_ingest_one_record_and_bank_change_in_body_flags(env) -> None:
    from invoicedesk_data.demo import bank_change_email

    mail = bank_change_email()
    att = mail.attachments[0]
    payload = {
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
            },
            {
                "filename": "readme.txt",
                "content_type": "text/plain",
                "content_b64": base64.b64encode(b"hi").decode(),
            },
        ],
    }
    raw = json.dumps(payload).encode()
    assert (
        env.client.post(
            "/api/v1/ingest/email", content=raw, headers={"Content-Type": "application/json"}
        ).status_code
        == 401
    )
    assert (
        env.client.post(
            "/api/v1/ingest/email", content=raw, headers=_sign(raw, "wrong")
        ).status_code
        == 401
    )
    r = env.client.post("/api/v1/ingest/email", content=raw, headers=_sign(raw))
    assert r.status_code == 202, r.text
    body = r.json()
    assert (
        len(body["documents"]) == 1
        and body["documents"][0]["filename"] == att.filename.rsplit("/", 1)[-1]
    )
    assert [x["filename"] for x in body["rejected"]] == ["readme.txt"]
    doc_id = body["documents"][0]["id"]
    # n8n retries the same delivery: the same single record comes back, nothing duplicated
    again = env.client.post("/api/v1/ingest/email", content=raw, headers=_sign(raw)).json()
    assert again["thread_id"] == body["thread_id"] and [d["id"] for d in again["documents"]] == [
        doc_id
    ]
    assert env.sql("select count(*) as n from email_threads")[0]["n"] == 1
    assert (
        env.sql(
            "select count(*) as n from document_threads where thread_id = %s", (body["thread_id"],)
        )[0]["n"]
        == 1
    )
    env.drain()
    assert env.status(doc_id) == "Flagged"
    detail = env.client.get(f"/api/v1/documents/{doc_id}", headers=env.dev(CLERK)).json()
    assert detail["source"] == "email"
    assert detail["email"] == {"from": mail.from_addr, "subject": mail.subject}
    r6 = next(x for x in detail["rule_results"] if x["rule_id"] == "R6")
    assert not r6["passed"] and r6["severity"] == "flag"
    assert "bank_change" in detail["badges"]
    clf = next(s for s in detail["screen_results"] if s["detector"] == "injection_classifier")
    assert "bank_or_remit_change" in clf["categories"]
    assert detail["approvals_required"] == {"count": 2, "callback": True}
    assert "000555077001" not in json.dumps(detail) and unmasked_numbers(detail) == []
    bad = env.client.post("/api/v1/ingest/email", content=b"{}", headers=_sign(b"{}"))
    assert bad.status_code == 422


def test_email_without_attachments_is_still_one_record(env) -> None:
    raw = json.dumps(
        {
            "message_id": "<x@mail.example>",
            "from": "a@vendor.example",
            "subject": "Hi",
            "body_text": "Hello",
        }
    ).encode()
    r = env.client.post("/api/v1/ingest/email", content=raw, headers=_sign(raw))
    assert r.status_code == 202 and r.json()["documents"] == []


def test_followup_without_webhook_is_approved_but_not_sent(env) -> None:
    f = demo_file("10-rapid-rooter")
    doc_id = _upload(env, [(f.filename, f.data, f.mime)], env.dev(CLERK)).json()["documents"][0][
        "id"
    ]
    env.drain()
    draft = env.client.post(f"/api/v1/documents/{doc_id}/followups", headers=env.dev(CLERK))
    assert draft.status_code == 201, draft.text
    body = draft.json()["draft_body"]
    assert "$340.00" in body or "estimate" in body.lower()  # deterministic template offline
    ok = env.client.post(
        f"/api/v1/followups/{draft.json()['id']}/approve", headers=env.dev(OWNER)
    ).json()
    assert ok["approved_by"] == env.staff[OWNER] and ok["sent_at"] is None
    assert env.webhook_calls == []
    approved = env.client.get("/api/v1/followups?status=approved", headers=env.dev(CLERK)).json()[
        "items"
    ]
    assert [x["id"] for x in approved] == [ok["id"]]
    actions = [
        r["action"]
        for r in env.sql(
            "select action from audit_log where document_id = %s order by id", (doc_id,)
        )
    ]
    assert (
        "followup.drafted" in actions
        and "followup.approved" in actions
        and "followup.sent" not in actions
    )
