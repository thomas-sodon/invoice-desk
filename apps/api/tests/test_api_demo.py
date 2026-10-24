"""The 25-invoice demo batch end to end through the API (US-1, US-8, M1, M9, M12, M13).

One module-scoped database: the demo runs once (real preprocessor and OCR, real guardrails,
extractor and rules, the gold-backed OracleLLM, FakeAccounting) and the tests below read and
then act on it, in file order.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from invoicedesk.audit.chain import verify
from invoicedesk_api.testkit import (
    APPROVER,
    CLERK,
    DEMO_ACCOUNT_NUMBERS,
    OWNER,
    Env,
    build_env,
    unmasked_numbers,
)

pytestmark = pytest.mark.postgres

GOLD_DIR = Path(__file__).resolve().parents[3] / "data" / "gold" / "demo"
EXPECTED = json.loads(
    (Path(__file__).resolve().parents[3] / "data" / "demo" / "expected.json").read_text()
)
STATUS_BY_FILE = {f["filename"]: f["status"] for f in EXPECTED["files"]}

DOC_KEYS = {
    "id", "batch_id", "filename", "source", "status", "column", "vendor_name", "invoice_number",
    "total_cents", "headline", "badges", "cost_usd", "thumbnail_url", "created_at", "updated_at",
}  # fmt: skip
DETAIL_KEYS = DOC_KEYS | {
    "pages", "original_url", "fields", "line_items", "rule_results", "screen_results",
    "embedded_instructions", "extractions", "approvals", "approvals_required", "bill",
    "followups", "email", "trace_url", "uploaded_by", "redactions",
}  # fmt: skip
FIELD_KEYS = {
    "name", "value", "display_value", "raw_conf", "calibrated_conf", "band", "page", "box",
    "word_ids", "reason",
}  # fmt: skip


@pytest.fixture(scope="module")
def demo(module_db, tmp_path_factory) -> Iterator[dict[str, Any]]:
    env = build_env(
        module_db,
        tmp_path_factory.mktemp("demo-storage"),
        api={"n8n_followup_webhook_url": "http://n8n.test/webhook/followup"},
    )
    resp = env.client.post("/api/v1/demo/run", json={"mode": "replay"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    ran = env.drain()
    by_name: dict[str, str] = {}
    for d in env.sql("select id, filename from documents where batch_id = %s", (body["batch_id"],)):
        by_name[d["filename"]] = str(d["id"])
    try:
        yield {
            "env": env,
            "batch_id": body["batch_id"],
            "ids": body["document_ids"],
            "by_name": by_name,
            "jobs": ran,
        }
    finally:
        env.client.close()
        env.rt.close()


def _env(demo: dict[str, Any]) -> Env:
    return demo["env"]


def _id(demo: dict[str, Any], prefix: str) -> str:
    return next(v for k, v in demo["by_name"].items() if k.startswith(prefix))


def _ids_with_status(demo: dict[str, Any], status: str) -> list[str]:
    rows = _env(demo).sql(
        "select id from documents where batch_id = %s and status = %s order by created_at, id",
        (demo["batch_id"], status),
    )
    return [str(r["id"]) for r in rows]


# ---------------------------------------------------------------------------- settle
def test_demo_run_settles_to_18_4_3(demo) -> None:
    env = _env(demo)
    view = env.client.get(f"/api/v1/batches/{demo['batch_id']}").json()
    assert view["counts"] == {"ready_to_pay": 18, "needs_review": 4, "flagged": 3}


def test_demo_board_matches_expected(demo) -> None:
    """The batch settles and every document routes exactly as labelled (data/demo/expected.json)."""
    env = _env(demo)
    assert len(demo["ids"]) == 25 and demo["jobs"] == 25
    view = env.client.get(f"/api/v1/batches/{demo['batch_id']}").json()  # anonymous: public demo
    assert view["batch"]["mode"] == "demo"
    assert set(view["documents"][0]) == DOC_KEYS
    got = {d["filename"]: d["status"] for d in view["documents"]}
    assert got == STATUS_BY_FILE
    # offline replay run: the oracle's synthetic usage is priced from the real price table
    assert float(view["cost_usd"]) > 0


def test_headlines_and_badges(demo) -> None:
    env = _env(demo)
    cards = {
        d["filename"]: d
        for d in env.client.get(f"/api/v1/documents?batch_id={demo['batch_id']}&limit=200").json()[
            "items"
        ]
    }
    over = cards["10-rapid-rooter-INV-2240-pdf.pdf"]
    assert over["headline"] == "$340.00 over approved estimate for Unit 12B (WO-1187)"
    assert "over_estimate" in over["badges"]
    dup = cards["21-rapid-rooter-INV-2236-photo.jpg"]
    assert "INV-2231" in dup["headline"] and "duplicate" in dup["badges"]
    assert dup["total_cents"] == 124000 and dup["column"] == "flagged"
    white = cards["18-ember-hearth-2900-B-pdf.pdf"]
    assert "hidden_text" in white["badges"] and white["headline"]
    faint = cards["13-kettle-creek-A-3600-photo.jpg"]
    assert "faint_text" in faint["badges"]
    assert "new_vendor" in cards["11-marigold-1043-scan.jpg"]["badges"]
    clean = next(c for c in cards.values() if c["status"] == "ReadyToPay")
    assert clean["headline"] is None and clean["column"] == "ready_to_pay"
    assert clean["thumbnail_url"].startswith("http://testserver/files/")


def test_board_pagination_and_filters(demo) -> None:
    env = _env(demo)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        q = f"/api/v1/documents?batch_id={demo['batch_id']}&limit=10" + (
            f"&cursor={cursor}" if cursor else ""
        )
        page = env.client.get(q).json()
        seen.extend(d["id"] for d in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and len(seen) == 25 and len(set(seen)) == 25
    flagged = env.client.get(f"/api/v1/documents?batch_id={demo['batch_id']}&column=flagged").json()
    assert len(flagged["items"]) == 3
    nr = env.client.get(f"/api/v1/documents?batch_id={demo['batch_id']}&status=NeedsReview").json()
    assert len(nr["items"]) == len(_ids_with_status(demo, "NeedsReview")) >= 4
    assert all(d["status"] == "NeedsReview" for d in nr["items"])
    assert (
        env.client.get("/api/v1/documents?column=nope", headers=env.dev(CLERK)).status_code == 422
    )
    # the tenant-wide board needs a staff login; the demo batch is public
    assert env.client.get("/api/v1/documents").status_code == 401
    assert env.client.get("/api/v1/documents", headers=env.dev(CLERK)).status_code == 200


def test_document_detail_shape_and_anonymous_redaction(demo) -> None:
    env = _env(demo)
    doc_id = _id(demo, "10-rapid-rooter")
    staff = env.client.get(f"/api/v1/documents/{doc_id}", headers=env.dev(CLERK)).json()
    assert set(staff) == DETAIL_KEYS
    assert set(staff["fields"][0]) == FIELD_KEYS
    assert staff["original_url"] and staff["pages"]
    assert max(staff["pages"][0]["width"], staff["pages"][0]["height"]) == 1600
    assert {r["rule_id"] for r in staff["rule_results"]} == {f"R{i}" for i in range(1, 11)}
    r3 = next(r for r in staff["rule_results"] if r["rule_id"] == "R3")
    assert r3["name"] == "three_way_match" and not r3["passed"] and r3["severity"] == "review"
    total = next(f for f in staff["fields"] if f["name"] == "total")
    assert total["band"] == "amber" and total["reason"].startswith("$340.00 over")
    assert total["display_value"].startswith("$") and total["value"].isdigit()
    assert staff["extractions"] and staff["extractions"][0]["tier"] == 1
    assert isinstance(staff["extractions"][0]["cost_usd"], str)
    assert staff["line_items"] and isinstance(staff["line_items"][0]["amount_cents"], int)
    assert staff["approvals_required"] is None and staff["bill"] is None
    # anonymous view of the public demo: no bank details, original, trace or approvals
    anon = env.client.get(f"/api/v1/documents/{doc_id}").json()
    assert set(anon) == DETAIL_KEYS
    assert anon["original_url"] is None and anon["trace_url"] is None and anon["approvals"] == []
    assert all(f["name"] != "remit_to_bank_details" for f in anon["fields"])
    # the signed page URL serves the 1,600 px render; the original is fetchable by staff
    page = env.client.get(staff["pages"][0]["image_url"].replace("http://testserver", ""))
    assert page.status_code == 200 and page.headers["content-type"] == "image/png"
    orig = env.client.get(staff["original_url"].replace("http://testserver", ""))
    assert orig.status_code == 200 and orig.content[:4] == b"%PDF"


def test_flagged_details_and_screening(demo) -> None:
    env = _env(demo)
    white = env.client.get(
        f"/api/v1/documents/{_id(demo, '18-ember')}", headers=env.dev(CLERK)
    ).json()
    assert white["status"] == "Flagged"
    assert white["approvals_required"] == {"count": 2, "callback": True}
    hidden = next(s for s in white["screen_results"] if s["detector"] == "hidden_text")
    assert hidden["verdict"] != "clean" and hidden["spans"]
    text = json.dumps(white)
    for acct in DEMO_ACCOUNT_NUMBERS:
        assert acct not in text
    assert white["extractions"] == []  # quarantined before extraction (invariant 8)


def test_audit_endpoint_chain_ok(demo) -> None:
    env = _env(demo)
    doc_id = _id(demo, "21-rapid-rooter")
    assert env.client.get(f"/api/v1/documents/{doc_id}/audit").status_code == 401
    audit = env.client.get(f"/api/v1/documents/{doc_id}/audit", headers=env.dev(CLERK)).json()
    assert audit["chain_ok"] is True
    actions = [e["action"] for e in audit["entries"]]
    assert actions[0] == "document.created" and "rule.R4" in actions
    assert set(audit["entries"][0]) == {
        "id",
        "ts",
        "actor",
        "action",
        "before",
        "after",
        "row_hash",
    }


def test_comparison_mode(demo) -> None:
    env = _env(demo)
    comp = env.client.get(f"/api/v1/batches/{demo['batch_id']}/comparison").json()
    assert len(comp["rows"]) == 25
    s = comp["summary"]
    assert s["invoicedesk_wrong"] == 0
    assert s["naive_wrong"] >= 2 and s["naive_dollars_lost_cents"] >= 124000
    row = next(r for r in comp["rows"] if r["filename"].startswith("21-rapid-rooter"))
    assert row["naive"]["verdict"] == "wrong" and row["invoicedesk"]["verdict"] == "correct"
    assert set(row) == {"document_id", "filename", "naive", "invoicedesk"}


def test_dashboard(demo) -> None:
    env = _env(demo)
    assert env.client.get("/api/v1/dashboard").status_code == 401
    d = env.client.get("/api/v1/dashboard?days=30", headers=env.dev(OWNER)).json()
    ready = len(_ids_with_status(demo, "ReadyToPay"))
    assert d["invoices"] == 25
    assert d["hands_free_pct"] == round(100 * ready / 25, 1)
    assert d["hours_saved"] == round(ready * 8 / 60, 1)
    assert d["dollars_caught_cents"]["duplicates"] == 124000
    assert d["dollars_caught_cents"]["overcharges"] == 34000
    assert d["dollars_caught_cents"]["total"] == sum(
        d["dollars_caught_cents"][k] for k in ("duplicates", "fraud", "overcharges")
    )
    assert d["manual_benchmark_per_invoice_cents"] == 990
    assert d["assumptions"] == {"minutes_per_invoice": 8}
    assert d["label"] == "benchmark on synthetic data"
    assert isinstance(d["cost_per_invoice_usd"], str) and float(d["cost_per_invoice_usd"]) > 0


# ---------------------------------------------------------------------------- actions
def test_two_person_release_of_the_duplicate(demo) -> None:
    env = _env(demo)
    doc_id = _id(demo, "21-rapid-rooter")
    url = f"/api/v1/documents/{doc_id}/approvals"
    body = {
        "kind": "duplicate",
        "decision": "approve",
        "callback_attested": True,
        "note": "Called 555-0100",
    }
    # clerks cannot vote
    r = env.client.post(url, json=body, headers=env.dev(CLERK))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    one = env.client.post(url, json=body, headers=env.dev(APPROVER))
    assert one.status_code == 200, one.text
    assert one.json()["status"] == "Flagged"
    again = env.client.post(url, json=body, headers=env.dev(APPROVER))
    assert again.status_code == 409 and again.json()["error"]["code"] == "duplicate_vote"
    two = env.client.post(url, json=body, headers=env.dev(OWNER)).json()
    assert two["status"] == "ReadyToPay"
    assert {a["approver_id"] for a in two["approvals"]} == {env.staff[APPROVER], env.staff[OWNER]}
    assert set(two["approvals"][0]) >= {
        "kind",
        "approver_id",
        "decision",
        "callback_attested",
        "created_at",
    }


def test_quarantined_document_is_never_released(demo) -> None:
    env = _env(demo)
    doc_id = _id(demo, "18-ember")
    url = f"/api/v1/documents/{doc_id}/approvals"
    body = {"kind": "injection", "decision": "approve", "callback_attested": True}
    assert env.client.post(url, json=body, headers=env.dev(APPROVER)).json()["status"] == "Flagged"
    assert env.client.post(url, json=body, headers=env.dev(OWNER)).json()["status"] == "Flagged"
    rej = env.client.post(
        f"/api/v1/documents/{doc_id}/reject", json={"reason": "Attack"}, headers=env.dev(CLERK)
    )
    assert rej.status_code == 200 and rej.json() == {"status": "Rejected"}


def test_correction_revalidates(demo) -> None:
    env = _env(demo)
    doc_id = _id(demo, "03-thistledown")
    bad = env.client.post(
        f"/api/v1/documents/{doc_id}/corrections",
        json={"corrections": [{"field": "remit_to_bank_details", "value": "x"}]},
        headers=env.dev(CLERK),
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_correction"
    detail = env.client.get(f"/api/v1/documents/{doc_id}", headers=env.dev(CLERK)).json()
    total = next(f for f in detail["fields"] if f["name"] == "total")
    assert total["band"] in ("amber", "red") and total["reason"]
    gold = json.loads((GOLD_DIR / f"{_gold_id('03-thistledown')}.json").read_text())
    r = env.client.post(
        f"/api/v1/documents/{doc_id}/corrections",
        json={"corrections": [{"field": "total", "value": str(gold["truth"]["total_cents"])}]},
        headers=env.dev(CLERK),
    )
    assert r.status_code == 200, r.text
    rules = {
        x["rule_id"]: x
        for x in env.client.get(f"/api/v1/documents/{doc_id}", headers=env.dev(CLERK)).json()[
            "rule_results"
        ]
    }
    assert rules["R5"]["passed"] and rules["R7"]["passed"]  # re-validated with the corrected total
    if r.json()["status"] == "NeedsReview":  # R9 still holds fields the oracle could not cite
        fields = rules["R9"]["fields"]
        corr = [{"field": f, "value": _gold_value(gold, f)} for f in fields]
        r = env.client.post(
            f"/api/v1/documents/{doc_id}/corrections",
            json={"corrections": corr},
            headers=env.dev(CLERK),
        )
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "ReadyToPay"
    again = env.client.post(
        f"/api/v1/documents/{doc_id}/corrections",
        json={"corrections": [{"field": "total", "value": "1"}]},
        headers=env.dev(CLERK),
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "illegal_transition"
    reviews = env.sql("select count(*) as n from reviews where document_id = %s", (doc_id,))
    assert reviews[0]["n"] >= 1
    # demo (seed) documents never become eval examples (excluded from every split)
    assert (
        env.sql("select count(*) as n from eval_examples where document_id = %s", (doc_id,))[0]["n"]
        == 0
    )


def _gold_value(gold: dict[str, Any], name: str) -> str:
    f = gold["extraction"][name]
    v = f.get("value", f.get("value_cents", f.get("value_iso")))
    return str(v)


def _gold_id(prefix: str) -> str:
    return next(f["gold_id"] for f in EXPECTED["files"] if f["filename"].startswith(prefix))


def test_followup_draft_approve_send(demo) -> None:
    env = _env(demo)
    nv = env.client.post(
        f"/api/v1/documents/{_id(demo, '11-marigold')}/followups", headers=env.dev(CLERK)
    )
    assert nv.status_code == 422 and nv.json()["error"]["code"] == "no_vendor"
    doc_id = _id(demo, "10-rapid-rooter")
    r = env.client.post(f"/api/v1/documents/{doc_id}/followups", headers=env.dev(CLERK))
    assert r.status_code == 201, r.text
    f = r.json()
    assert set(f) == {
        "id",
        "document_id",
        "to_addr",
        "draft_subject",
        "draft_body",
        "approved_by",
        "sent_at",
    }
    vendor_email = env.sql(
        "select email from vendors where legal_name = 'Rapid Rooter Plumbing LLC'"
    )[0]["email"]
    assert f["to_addr"] == vendor_email and f["approved_by"] is None
    drafts = env.client.get("/api/v1/followups?status=draft", headers=env.dev(CLERK)).json()[
        "items"
    ]
    assert [d["id"] for d in drafts] == [f["id"]]
    assert (
        env.client.post(
            f"/api/v1/followups/{f['id']}/approve", json={}, headers=env.dev(CLERK)
        ).status_code
        == 403
    )
    ok = env.client.post(
        f"/api/v1/followups/{f['id']}/approve",
        json={"subject": "Invoice INV-2240: estimate question"},
        headers=env.dev(APPROVER),
    ).json()
    assert ok["approved_by"] == env.staff[APPROVER] and ok["sent_at"] is not None
    assert ok["draft_subject"] == "Invoice INV-2240: estimate question"
    assert env.webhook_calls and env.webhook_calls[-1]["json"]["to"] == vendor_email
    assert (
        env.client.get("/api/v1/followups?status=sent", headers=env.dev(CLERK)).json()["items"][0][
            "id"
        ]
        == f["id"]
    )
    assert (
        env.client.get("/api/v1/followups?status=draft", headers=env.dev(CLERK)).json()["items"]
        == []
    )
    resend = env.client.post(
        f"/api/v1/followups/{f['id']}/approve", json={}, headers=env.dev(APPROVER)
    )
    assert resend.status_code == 409
    detail = env.client.get(f"/api/v1/documents/{doc_id}", headers=env.dev(CLERK)).json()
    assert detail["followups"][0]["id"] == f["id"]


def test_post_one_then_post_ready(demo) -> None:
    env = _env(demo)
    nr = _id(demo, "10-rapid-rooter")
    r = env.client.post(f"/api/v1/documents/{nr}/post", headers=env.dev(CLERK))
    assert r.status_code == 409 and r.json()["error"]["code"] == "illegal_transition"
    assert env.client.post(f"/api/v1/documents/{nr}/post").status_code == 401
    ready = _ids_with_status(demo, "ReadyToPay")
    # includes the two-person-released duplicate and the corrected handwritten invoice
    assert _id(demo, "21-rapid-rooter") in ready and _id(demo, "03-thistledown") in ready
    one = ready[0]
    job = env.client.post(f"/api/v1/documents/{one}/post", headers=env.dev(CLERK))
    assert job.status_code == 202 and job.json()["job_id"]
    env.drain()
    detail = env.client.get(f"/api/v1/documents/{one}", headers=env.dev(CLERK)).json()
    assert detail["status"] == "Posted" and detail["bill"]["status"] == "posted"
    assert detail["bill"]["external_id"] and detail["column"] == "posted"
    out = env.client.post(
        f"/api/v1/batches/{demo['batch_id']}/post-ready", headers=env.dev(CLERK)
    ).json()
    assert out["count"] == len(ready) - 1 and len(out["job_ids"]) == out["count"]
    env.drain()
    view = env.client.get(f"/api/v1/batches/{demo['batch_id']}", headers=env.dev(CLERK)).json()
    assert view["counts"]["posted"] == len(ready)
    assert len(env.accounting.bills) == len(ready)  # exactly one bill per posted document
    again = env.client.post(f"/api/v1/documents/{one}/post", headers=env.dev(CLERK))
    assert again.status_code == 409
    with env.db.tx() as conn:
        assert verify(conn) == (True, None)


def test_sse_event_feed(demo) -> None:
    import threading
    import time

    env = _env(demo)
    env.rt.sse_max_events = 1
    stop = threading.Event()

    def notify() -> None:
        payload = json.dumps(
            {
                "type": "document.status_changed",
                "tenant_id": "maple_row",
                "document_id": demo["ids"][0],
                "batch_id": demo["batch_id"],
                "status": "Flagged",
            }
        )
        while not stop.is_set():
            with env.db.tx() as conn:
                conn.execute("select pg_notify('invoicedesk_events', %s)", (payload,))
            time.sleep(0.1)

    t = threading.Thread(target=notify, daemon=True)
    t.start()
    try:
        with env.client.stream("GET", f"/api/v1/events?batch_id={demo['batch_id']}") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            text = "".join(resp.iter_text())
    finally:
        stop.set()
        t.join(2)
        env.rt.sse_max_events = None
    data = [ln for ln in text.splitlines() if ln.startswith("data: ")]
    ev = json.loads(data[0][6:])
    assert ev["column"] == "flagged" and ev["batch_id"] == demo["batch_id"]
    assert set(ev) == {
        "type",
        "tenant_id",
        "document_id",
        "batch_id",
        "status",
        "column",
        "cost_usd",
        "batch_cost_usd",
        "message",
        "ts",
    }
    assert env.client.get("/api/v1/events").status_code == 401


def test_no_bank_number_unmasked_in_any_response(demo) -> None:
    env = _env(demo)
    for doc_id in demo["ids"]:
        env.client.get(f"/api/v1/documents/{doc_id}", headers=env.dev(CLERK))
        env.client.get(f"/api/v1/documents/{doc_id}/audit", headers=env.dev(CLERK))
        env.client.get(f"/api/v1/documents/{doc_id}")
    env.client.get(f"/api/v1/batches/{demo['batch_id']}", headers=env.dev(CLERK))
    env.client.get("/api/v1/followups", headers=env.dev(CLERK))
    assert len(env.client.bodies) > 80
    raw = json.dumps(env.client.bodies)
    for acct in DEMO_ACCOUNT_NUMBERS:
        assert acct not in raw
    leaks = [s for body in env.client.bodies for s in unmasked_numbers(body)]
    assert leaks == []
