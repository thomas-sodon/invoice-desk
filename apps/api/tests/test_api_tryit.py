"""Public try-it page (M14, US-9): Turnstile, limits, $/day cap -> replay, never posted, purge."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from invoicedesk.audit.chain import verify
from invoicedesk.db import repo
from invoicedesk.status import Actor, IllegalTransition
from invoicedesk_api.purge import purge_expired
from invoicedesk_api.testkit import CLERK, demo_file, unmasked_numbers
from invoicedesk_api.turnstile import TEST_SECRET_ALWAYS_FAILS, TEST_SECRET_TOKEN_SPENT

pytestmark = pytest.mark.postgres

TOKEN = "XXXX.DUMMY.TOKEN.XXXX"  # what Cloudflare's test site keys return


def _session(env):
    r = env.client.post("/api/v1/tryit/session", json={"turnstile_token": TOKEN})
    assert r.status_code == 200, r.text
    return r


def _upload(env, f=None):
    f = f or demo_file("04-voltwise")
    return env.client.post("/api/v1/tryit/upload", files={"file": (f.filename, f.data, f.mime)})


@pytest.mark.parametrize("secret", [TEST_SECRET_ALWAYS_FAILS, TEST_SECRET_TOKEN_SPENT])
def test_turnstile_failure_is_403(env_factory, secret) -> None:
    env = env_factory(settings={"turnstile_secret_key": __import__("pydantic").SecretStr(secret)})
    r = env.client.post("/api/v1/tryit/session", json={"turnstile_token": TOKEN})
    assert r.status_code == 403 and r.json()["error"]["code"] == "turnstile_failed"
    assert "idsess" not in r.cookies


def test_session_status_upload_and_visitor_view(env) -> None:
    assert (
        env.client.post(
            "/api/v1/tryit/upload", files={"file": ("a.png", b"x", "image/png")}
        ).status_code
        == 401
    )
    s = _session(env)
    body = s.json()
    assert body["uploads_remaining"] == 3 and "redacted" in body["banner"]
    assert "idsess" in env.client.cookies
    st = env.client.get("/api/v1/tryit/status").json()
    assert st == {
        "spend_cap_reached": False,
        "uploads_remaining": 3,
        "replay_batch_id": None,
        "banner": st["banner"],
    }
    r = _upload(env, demo_file("13-kettle-creek"))  # carries a faint-text bank-change instruction
    assert r.status_code == 202, r.text
    doc_id = r.json()["document_id"]
    row = env.sql(
        "select session_id, purge_after, created_at, storage_path from documents where id = %s",
        (doc_id,),
    )[0]
    assert row["session_id"] == body["session_id"]
    assert row["purge_after"] - row["created_at"] == pytest.approx(
        dt.timedelta(hours=24), abs=dt.timedelta(seconds=5)
    )
    assert row["storage_path"].startswith(f"visitor/{body['session_id']}/")
    job = env.sql("select payload from jobs where document_id = %s", (doc_id,))[0]
    assert job["payload"]["max_tier"] == "sonnet"
    env.drain()
    d = env.client.get(f"/api/v1/tryit/documents/{doc_id}").json()
    assert d["status"] == "Flagged"
    assert d["original_url"] is None and d["trace_url"] is None and d["approvals"] == []
    assert all(f["name"] != "remit_to_bank_details" for f in d["fields"])
    assert unmasked_numbers(d) == []
    assert env.client.get("/api/v1/tryit/status").json()["uploads_remaining"] == 2
    # visitor uploads are not on the staff board, and another session cannot read them
    staff_board = env.client.get("/api/v1/documents", headers=env.dev(CLERK)).json()["items"]
    assert doc_id not in {x["id"] for x in staff_board}
    env.client.cookies.clear()
    _session(env)
    assert env.client.get(f"/api/v1/tryit/documents/{doc_id}").status_code == 404
    env.client.cookies.clear()
    assert env.client.get(f"/api/v1/tryit/documents/{doc_id}").status_code == 401


def test_tampered_cookie_is_ignored(env) -> None:
    env.client.cookies.set("idsess", "forged.value.sig")
    r = _upload(env)
    assert r.status_code == 401


def test_visitor_upload_is_never_posted(env) -> None:
    _session(env)
    doc_id = _upload(env).json()["document_id"]
    env.drain()
    assert env.status(doc_id) == "ReadyToPay"
    r = env.client.post(f"/api/v1/documents/{doc_id}/post", headers=env.dev(CLERK))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    with pytest.raises(IllegalTransition):
        env.rt.services().post_document(doc_id, actor=Actor.user(env.staff[CLERK]))
    from invoicedesk.status import DocStatus, PostingReceipt

    with env.db.tx() as conn, pytest.raises(IllegalTransition):
        repo.set_status(
            conn,
            doc_id,
            DocStatus.POSTED,
            actor=Actor.system("x"),
            reason="x",
            evidence=PostingReceipt("fake", "1", "k"),
        )
    assert env.accounting.bills == {}
    out = env.client.post(
        f"/api/v1/documents/{doc_id}/approvals",
        json={"kind": "other", "decision": "approve", "callback_attested": True},
        headers=env.dev("priya.approver@maplerow.example"),
    )
    assert out.status_code == 403


def test_per_session_and_per_ip_limits(env_factory) -> None:
    env = env_factory(
        api={
            "tryit_uploads_per_session": 1,
            "tryit_uploads_per_ip_per_day": 2,
            "tryit_sessions_per_ip_per_day": 3,
        }
    )
    _session(env)
    assert _upload(env).status_code == 202
    second = _upload(env)
    assert second.status_code == 429 and second.json()["error"]["code"] == "rate_limited"
    env.client.cookies.clear()
    _session(env)
    assert _upload(env).status_code == 202
    env.client.cookies.clear()
    _session(env)
    third = _upload(env)  # a fresh session, but the IP used its 2 uploads today
    assert third.status_code == 429
    # the refused IP upload did not consume the new session's quota
    assert env.client.get("/api/v1/tryit/status").json()["uploads_remaining"] == 1
    env.client.cookies.clear()
    assert (
        env.client.post("/api/v1/tryit/session", json={"turnstile_token": TOKEN}).status_code == 429
    )
    # rejected files are refused before any quota is taken
    env2 = env_factory(api={"tryit_uploads_per_session": 1})
    _session(env2)
    bad = env2.client.post(
        "/api/v1/tryit/upload", files={"file": ("x.txt", b"hello", "text/plain")}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "rejected_input"
    assert _upload(env2).status_code == 202


def test_spend_cap_trips_to_replay(env) -> None:
    _session(env)
    with env.db.tx() as conn:
        repo.add_spend(conn, scope="global", key="tryit", usd=Decimal("2.95"))
    st = env.client.get("/api/v1/tryit/status").json()
    assert st["spend_cap_reached"] is True and st["uploads_remaining"] == 0
    replay_id = st["replay_batch_id"]
    assert replay_id
    r = _upload(env)
    assert r.status_code == 200
    assert r.json() == {"replay": True, "replay_batch_id": replay_id}
    batch = env.sql("select mode from batches where id = %s", (replay_id,))[0]
    assert batch["mode"] == "demo"
    assert (
        env.sql("select llm from demo_runs where batch_id = %s", (replay_id,))[0]["llm"] == "replay"
    )
    # nothing was stored for the refused upload
    assert env.sql("select count(*) as n from documents where session_id is not null")[0]["n"] == 0
    # the replay board is public
    assert env.client.get(f"/api/v1/batches/{replay_id}").status_code == 200


def test_visitor_spend_is_metered_by_the_pipeline(env) -> None:
    sid = _session(env).json()["session_id"]
    _upload(env)
    env.drain()
    with env.db.tx() as conn:
        g = repo.get_spend(conn, scope="global", key="tryit")
        s = repo.get_spend(conn, scope="session", key=sid)
    assert g > 0 and s == g


def test_purge_deletes_expired_visitor_uploads(env) -> None:
    _session(env)
    doc_id = _upload(env).json()["document_id"]
    env.drain()
    keep = env.client.post(
        "/api/v1/documents",
        files=[
            (
                "files[]",
                (
                    demo_file("04-voltwise").filename,
                    demo_file("04-voltwise").data,
                    "application/pdf",
                ),
            )
        ],
        headers=env.dev(CLERK),
    ).json()["documents"][0]["id"]
    row = env.sql("select session_id, storage_path from documents where id = %s", (doc_id,))[0]
    keys = env.rt.storage.list(f"visitor/{row['session_id']}/")
    assert row["storage_path"] in keys and any("/renders/" in k for k in keys)
    assert purge_expired(env.db, env.rt.storage).documents == []  # not expired yet
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=25)
    report = purge_expired(env.db, env.rt.storage, now=later)
    assert report.documents == [doc_id] and report.objects_deleted == len(keys)
    assert env.rt.storage.list(f"visitor/{row['session_id']}/") == []
    assert env.sql("select count(*) as n from documents where id = %s", (doc_id,))[0]["n"] == 0
    assert env.sql("select count(*) as n from documents where id = %s", (keep,))[0]["n"] == 1
    audit = env.sql("select action from audit_log where document_id = %s order by id", (doc_id,))
    assert audit[0]["action"] == "document.created" and audit[-1]["action"] == "document.purged"
    assert (
        env.sql("select count(*) as n from checkpoints where thread_id = %s", (doc_id,))[0]["n"]
        == 0
    )
    with env.db.tx() as conn:
        assert verify(conn) == (True, None)


def test_purge_script_entry_point(env, monkeypatch) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "scripts" / "purge_visitor_uploads.py"
    spec = importlib.util.spec_from_file_location("purge_script", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--dsn", env.db.dsn, "--dry-run"]) == 0
