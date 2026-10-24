"""Scorecard, report card, signed files, demo re-run, and the offline/live adapters (Turnstile,
SSRF-guarded fetch, Supabase Storage) against mocked transports."""

from __future__ import annotations

import datetime as dt
import json
import time

import httpx
import pytest
from invoicedesk.contracts.preprocess import RejectedInput
from invoicedesk_api.fetch import HttpsFetcher, check_public_https_url
from invoicedesk_api.routes.events import normalize_event
from invoicedesk_api.storage_supabase import SupabaseStorage, SupabaseStorageError
from invoicedesk_api.testkit import CLERK, OWNER
from invoicedesk_api.turnstile import (
    TEST_SECRET_ALWAYS_PASSES,
    CloudflareTurnstile,
    OfflineTurnstile,
    build_turnstile,
)
from invoicedesk_api.uploads import safe_filename

# ---------------------------------------------------------------------------- pure units


def test_turnstile_offline_and_cloudflare() -> None:
    assert OfflineTurnstile(None).verify("tok", remote_ip=None).success
    assert not OfflineTurnstile(None).verify("", remote_ip=None).success
    assert not OfflineTurnstile("real-secret").verify("tok", remote_ip=None).success
    seen: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        form = dict(x.split("=") for x in req.content.decode().split("&"))
        seen.append(form)
        ok = form["response"] == "good"
        return httpx.Response(
            200, json={"success": ok, "error-codes": [] if ok else ["invalid-input-response"]}
        )

    cf = CloudflareTurnstile("s3cret", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert cf.verify("good", remote_ip="203.0.113.9").success
    bad = cf.verify("bad", remote_ip=None)
    assert not bad.success and bad.error_codes == ("invalid-input-response",)
    assert seen[0] == {"secret": "s3cret", "response": "good", "remoteip": "203.0.113.9"}
    down = CloudflareTurnstile(
        "s",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="x"))),
    )
    assert not down.verify("good", remote_ip=None).success  # fails closed
    assert isinstance(build_turnstile("live", None), OfflineTurnstile)
    assert not build_turnstile("live", None).verify("t", remote_ip=None).success
    assert build_turnstile("offline", None).verify("t", remote_ip=None).success
    assert OfflineTurnstile(TEST_SECRET_ALWAYS_PASSES).secret == TEST_SECRET_ALWAYS_PASSES


def test_fetch_ssrf_guards() -> None:
    public = lambda _h: ["93.184.216.34"]  # noqa: E731
    assert check_public_https_url("https://files.example/a.pdf", resolver=public) == "files.example"
    for url in (
        "http://files.example/a.pdf",
        "https://user:pw@files.example/a",
        "https://files.example:8443/a",
        "ftp://x/y",
    ):
        with pytest.raises(RejectedInput):
            check_public_https_url(url, resolver=public)
    for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "192.168.1.1", "100.64.0.1"):
        with pytest.raises(RejectedInput):
            check_public_https_url("https://evil.example/a", resolver=lambda _h, ip=ip: [ip])
    pdf = b"%PDF-1.7 tiny"
    big = b"0" * (10 * 1024 * 1024 + 10)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/ok.pdf":
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        if req.url.path == "/big":
            return httpx.Response(200, content=big)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/"})

    f = HttpsFetcher(http=httpx.Client(transport=httpx.MockTransport(handler)), resolver=public)
    assert f.fetch("https://files.example/ok.pdf") == (pdf, "application/pdf")
    with pytest.raises(RejectedInput):
        f.fetch("https://files.example/big")
    with pytest.raises(RejectedInput):
        f.fetch("https://files.example/redirect")  # redirects are never followed


def test_safe_filename() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\Users\\x\\Invoice #12.pdf") == "Invoice _12.pdf"
    assert safe_filename("") == "invoice"
    assert len(safe_filename("a" * 300 + ".pdf")) <= 120 and safe_filename(
        "a" * 300 + ".pdf"
    ).endswith(".pdf")


def test_normalize_event() -> None:
    ev = normalize_event(
        json.dumps(
            {
                "type": "document.status_changed",
                "tenant_id": "maple_row",
                "document_id": "d",
                "batch_id": None,
                "status": "NeedsReview",
                "extra": 1,
                "message": "acct 000123456789",
            }
        )
    )
    assert ev is not None and ev.column == "needs_review" and ev.ts
    assert ev.message == "acct ••••••••6789"
    assert normalize_event("not json") is None
    assert normalize_event(json.dumps({"type": "bogus", "tenant_id": "t"})) is None


def test_supabase_storage_against_mock_rest_api() -> None:
    store: dict[str, tuple[bytes, str]] = {}
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer svc" and req.headers["apikey"] == "svc"
        path = req.url.path.removeprefix("/storage/v1")
        calls.append((req.method, path))
        if req.method == "POST" and path.startswith("/object/sign/invoices/"):
            key = path.removeprefix("/object/sign/invoices/")
            assert json.loads(req.content) == {"expiresIn": 60}
            return httpx.Response(200, json={"signedURL": f"/object/sign/invoices/{key}?token=t"})
        if req.method == "POST" and path == "/object/list/invoices":
            body = json.loads(req.content)
            prefix = body["prefix"]
            names: dict[str, bool] = {}
            for k in store:
                if k.startswith(prefix + "/"):
                    rest = k[len(prefix) + 1 :]
                    head, _, tail = rest.partition("/")
                    names[head] = bool(tail)
            return httpx.Response(
                200,
                json=[
                    {"name": n, "id": None if folder else "x"}
                    for n, folder in sorted(names.items())
                ],
            )
        if req.method == "POST" and path.startswith("/object/invoices/"):
            assert req.headers["x-upsert"] == "true"
            store[path.removeprefix("/object/invoices/")] = (
                req.content,
                req.headers["content-type"],
            )
            return httpx.Response(200, json={"Key": path})
        if path.startswith("/object/authenticated/invoices/"):
            key = path.removeprefix("/object/authenticated/invoices/")
            if key not in store:
                return httpx.Response(400, json={"error": "not_found"})
            return httpx.Response(200, content=b"" if req.method == "HEAD" else store[key][0])
        if req.method == "DELETE" and path == "/object/invoices":
            for k in json.loads(req.content)["prefixes"]:
                store.pop(k, None)
            return httpx.Response(200, json=[])
        return httpx.Response(500)

    s = SupabaseStorage(
        "https://proj.supabase.co", "svc", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    obj = s.put("maple_row/originals/d1/a.pdf", b"%PDF", content_type="application/pdf")
    s.put("maple_row/renders/d1/page-0.png", b"png", content_type="image/png")
    assert obj.size == 4 and s.get("maple_row/originals/d1/a.pdf") == b"%PDF"
    assert s.exists("maple_row/originals/d1/a.pdf") and not s.exists("maple_row/nope")
    assert s.list("maple_row/") == [
        "maple_row/originals/d1/a.pdf",
        "maple_row/renders/d1/page-0.png",
    ]
    assert s.signed_url("maple_row/originals/d1/a.pdf", expires_in_s=60) == (
        "https://proj.supabase.co/storage/v1/object/sign/invoices/maple_row/originals/d1/a.pdf?token=t"
    )
    s.delete("maple_row/originals/d1/a.pdf")
    with pytest.raises(KeyError):
        s.get("maple_row/originals/d1/a.pdf")
    with pytest.raises(ValueError):
        s.put("../escape", b"x", content_type="text/plain")
    broken = SupabaseStorage(
        "https://p",
        "svc",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    with pytest.raises(SupabaseStorageError):
        broken.put("a/b", b"x", content_type="text/plain")


def test_supabase_storage_from_settings_requires_keys() -> None:
    from invoicedesk.config.settings import Settings
    from pydantic import SecretStr

    base = Settings()  # pyright: ignore[reportCallIssue]
    with pytest.raises(RuntimeError):
        SupabaseStorage.from_settings(base.model_copy(update={"supabase_url": None}))
    ok = SupabaseStorage.from_settings(
        base.model_copy(
            update={
                "supabase_url": "https://p.supabase.co",
                "supabase_service_role_key": SecretStr("k"),
            }
        )
    )
    assert ok.base == "https://p.supabase.co/storage/v1"


# ---------------------------------------------------------------------------- endpoints
@pytest.mark.postgres
def test_scorecard_and_report_card(env_factory, tmp_path) -> None:
    out_dir = tmp_path / "evals-out"
    env = env_factory(api={"report_card_dir": out_dir})
    r = env.client.get("/api/v1/scorecard/latest")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    with env.db.tx() as conn:
        conn.execute(
            "insert into eval_runs (git_sha, config, mode, finished_at, totals_json) values"
            " ('abc123', 'D', 'live', now() - interval '1 day', %s),"
            " ('def456', 'D', 'live', now(), %s), ('zzz', 'A', 'live', null, '{}'),"
            " ('smoke', 'D', 'offline', now() + interval '1 hour', '{}'),"
            " ('later-a', 'A', 'live', now() + interval '1 hour', '{}')",
            (
                json.dumps({"metrics": {"field_f1": {"value": 0.9, "ci": [0.88, 0.92]}}}),
                json.dumps(
                    {
                        "metrics": {
                            "field_f1": {"value": 0.95, "ci": [0.93, 0.97]},
                            "stp": 0.76,
                            "usd_per_doc": "0.011",
                        },
                        "reliability": [{"confidence": 0.9, "accuracy": 0.88, "count": 40}],
                    }
                ),
            ),
        )
    sc = env.client.get("/api/v1/scorecard/latest").json()
    assert sc["git_sha"] == "def456" and sc["config"] == "D"
    assert sc["metrics"]["field_f1"] == {"value": 0.95, "ci": [0.93, 0.97]}
    assert sc["metrics"]["stp"] == {"value": 0.76, "ci": None}
    assert sc["label"] == "benchmark on synthetic data" and sc["reliability"][0]["count"] == 40
    assert set(sc) == {
        "run_id",
        "git_sha",
        "config",
        "finished_at",
        "metrics",
        "label",
        "reliability",
    }
    # with no live run, the latest offline run is shown, labelled as an offline harness run
    with env.db.tx() as conn:
        conn.execute("delete from eval_runs where mode = 'live'")
    off = env.client.get("/api/v1/scorecard/latest").json()
    assert off["git_sha"] == "smoke" and "offline harness run" in off["label"]
    assert env.client.get("/api/v1/report-card.pdf").status_code == 404
    out_dir.mkdir()
    (out_dir / "report-card-old.pdf").write_bytes(b"%PDF-old")
    time.sleep(0.01)
    (out_dir / "run2").mkdir()
    (out_dir / "run2" / "report-card.pdf").write_bytes(b"%PDF-new")
    pdf = env.client.get("/api/v1/report-card.pdf")
    assert pdf.status_code == 200 and pdf.content == b"%PDF-new"
    assert pdf.headers["content-type"] == "application/pdf"


@pytest.mark.postgres
def test_signed_file_urls(env) -> None:
    key = "maple_row/originals/x/inv.pdf"
    env.rt.storage.put(key, b"%PDF-1.7", content_type="application/pdf")
    url = env.rt.storage.signed_url(key, expires_in_s=60).replace("http://testserver", "")
    for prefix in ("", "/api/v1"):
        r = env.client.get(prefix + url)
        assert r.status_code == 200 and r.content == b"%PDF-1.7"
    tampered = url.replace("inv.pdf", "other.pdf")
    assert env.client.get(tampered).status_code == 403
    exp = int(time.time()) - 10
    sig = env.rt.storage.sign(key, exp)  # type: ignore[attr-defined]
    expired = env.client.get(f"/files/{key}?exp={exp}&sig={sig}")
    assert expired.status_code == 403 and expired.json()["error"]["code"] == "forbidden"
    assert env.client.get(f"/files/{key}").status_code == 422


@pytest.mark.postgres
def test_demo_run_rules(env_factory) -> None:
    env = env_factory(api={"tryit_demo_runs_per_ip_per_day": 1})
    first = env.client.post("/api/v1/demo/run", json={"mode": "live"})  # anonymous -> replay
    assert first.status_code == 202 and len(first.json()["document_ids"]) == 25
    bid = first.json()["batch_id"]
    assert env.sql("select llm from demo_runs where batch_id = %s", (bid,))[0]["llm"] == "replay"
    assert env.client.post("/api/v1/demo/run", json={"mode": "replay"}).status_code == 429
    # staff are not rate limited; a new run clears the previous demo batch (else R4 would flag
    # every file as a duplicate of the last run)
    second = env.client.post("/api/v1/demo/run", json={"mode": "live"}, headers=env.dev(CLERK))
    assert second.status_code == 202
    assert env.sql("select count(*) as n from batches where mode = 'demo'")[0]["n"] == 1
    assert env.sql("select count(*) as n from documents where batch_id = %s", (bid,))[0]["n"] == 0
    new = second.json()["batch_id"]
    # offline mode never calls a paid API, even when a staff user asks for "live"
    assert env.sql("select llm, requested_by from demo_runs where batch_id = %s", (new,))[0] == {
        "llm": "replay",
        "requested_by": f"user:{env.staff[CLERK]}",
    }
    uploaded = env.sql("select distinct uploaded_by from documents where batch_id = %s", (new,))
    assert [str(r["uploaded_by"]) for r in uploaded] == [env.staff[CLERK]]
    actions = [
        r["action"]
        for r in env.sql("select action from audit_log where action like 'demo.%%' order by id")
    ]
    assert actions == ["demo.run", "demo.reset", "demo.run"]


@pytest.mark.postgres
def test_demo_run_refuses_when_last_run_posted_to_qbo(env) -> None:
    first = env.client.post("/api/v1/demo/run", json={"mode": "replay"}).json()
    doc = first["document_ids"][0]
    with env.db.tx() as conn:
        conn.execute(
            "insert into bills (document_id, system, external_id, idempotency_key, status)"
            " values (%s, 'qbo', '991', 'k1', 'posted')",
            (doc,),
        )
    r = env.client.post("/api/v1/demo/run", json={"mode": "replay"}, headers=env.dev(CLERK))
    assert r.status_code == 409 and r.json()["error"]["code"] == "demo_reset_required"


@pytest.mark.postgres
def test_misc_errors_and_empty_dashboard(env) -> None:
    assert env.client.get("/healthz").json() == {"status": "ok"}
    d = env.client.get("/api/v1/dashboard", headers=env.dev(OWNER)).json()
    assert d["invoices"] == 0 and d["hands_free_pct"] == 0.0 and d["cost_per_invoice_usd"] == "0"
    assert env.client.get("/api/v1/dashboard?days=0", headers=env.dev(OWNER)).status_code == 422
    r = env.client.post(
        "/api/v1/batches", json={"label": "x", "mode": "bogus"}, headers=env.dev(CLERK)
    )
    assert (
        r.status_code == 422
        and set(r.json()) == {"error"}
        and r.json()["error"]["code"] == "validation_error"
    )
    assert (
        env.client.get(
            "/api/v1/batches/00000000-0000-0000-0000-000000000000", headers=env.dev(CLERK)
        ).status_code
        == 404
    )
    assert env.client.get("/api/v1/documents?cursor=@@@", headers=env.dev(CLERK)).status_code == 422
    assert (
        env.client.get("/api/v1/followups?status=bogus", headers=env.dev(CLERK)).status_code == 422
    )
    assert (
        env.client.post(
            "/api/v1/followups/00000000-0000-0000-0000-000000000000/approve",
            json={},
            headers=env.dev(OWNER),
        ).status_code
        == 404
    )
    assert (
        env.client.get(
            "/api/v1/batches/00000000-0000-0000-0000-000000000000/comparison"
        ).status_code
        == 404
    )
    batch = env.client.post(
        "/api/v1/batches", json={"label": "Oct"}, headers=env.dev(CLERK)
    ).json()["batch_id"]
    assert env.client.get(f"/api/v1/batches/{batch}/comparison").status_code == 401
    comp = env.client.get(f"/api/v1/batches/{batch}/comparison", headers=env.dev(CLERK)).json()
    assert comp["rows"] == [] and comp["summary"]["naive_wrong"] == 0
    assert env.client.get(f"/api/v1/batches/{batch}").status_code == 401
    view = env.client.get(f"/api/v1/batches/{batch}", headers=env.dev(CLERK)).json()
    assert view["counts"] == {} and view["cost_usd"] == "0" and view["documents"] == []
    assert dt.datetime.fromisoformat(view["batch"]["created_at"]).tzinfo is not None


@pytest.mark.postgres
def test_embedded_job_threads_process_uploads(env) -> None:
    from fastapi.testclient import TestClient
    from invoicedesk_api.main import create_app
    from invoicedesk_api.testkit import demo_file

    f = demo_file("04-voltwise")
    with TestClient(create_app(env.rt, start_jobs=True)) as client:
        assert env.rt.runner is not None
        r = client.post(
            "/api/v1/documents",
            files=[("files[]", (f.filename, f.data, f.mime))],
            headers=env.dev(CLERK),
        )
        doc_id = r.json()["documents"][0]["id"]
        deadline = time.monotonic() + 120
        while (
            env.status(doc_id) not in ("ReadyToPay", "NeedsReview", "Flagged")
            and time.monotonic() < deadline
        ):
            time.sleep(0.25)
        assert env.status(doc_id) == "ReadyToPay"

        def job_status() -> str:
            q = "select status::text as s from jobs where document_id = %s"
            return env.sql(q, (doc_id,))[0]["s"]

        # the job is completed just after the pipeline returns: poll, do not race it
        while job_status() == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert job_status() == "done"
    assert env.rt.runner is None  # stopped with the app


def test_blank_or_short_agent_secret_is_never_a_key(monkeypatch) -> None:
    """An empty ``AGENT_JWT_SECRET=`` copied from .env.example means "agent facade off", never
    an empty HS256 key; a short secret is refused at startup."""
    from invoicedesk_api.settings import ApiSettings
    from pydantic import ValidationError

    monkeypatch.setenv("AGENT_JWT_SECRET", "")
    assert ApiSettings().agent_jwt_secret is None  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("AGENT_JWT_SECRET", "   ")
    assert ApiSettings().agent_jwt_secret is None  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("AGENT_JWT_SECRET", "short")
    with pytest.raises(ValidationError):
        ApiSettings()  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("AGENT_JWT_SECRET", "x" * 32)
    secret = ApiSettings().agent_jwt_secret  # pyright: ignore[reportCallIssue]
    assert secret is not None and secret.get_secret_value() == "x" * 32


def test_client_ip_prefers_the_proxy_header_then_forwarded_for() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from invoicedesk_api.auth import client_ip

    def req(headers: dict[str, str]) -> Any:
        return SimpleNamespace(headers=headers, client=SimpleNamespace(host="10.0.0.1"))

    def rt(header: str | None, trust: bool) -> Any:
        return SimpleNamespace(
            api=SimpleNamespace(client_ip_header=header, trust_forwarded_for=trust)
        )

    spoof = {"x-forwarded-for": "6.6.6.6, 1.2.3.4"}
    assert client_ip(req(spoof), cast(Any, rt(None, False))) == "10.0.0.1"
    assert client_ip(req(spoof), cast(Any, rt(None, True))) == "6.6.6.6"
    fly = {**spoof, "Fly-Client-IP": "1.2.3.4"}
    assert client_ip(req(fly), cast(Any, rt("Fly-Client-IP", False))) == "1.2.3.4"
    # a missing or list-valued header falls back to the socket address
    assert client_ip(req(spoof), cast(Any, rt("Fly-Client-IP", False))) == "10.0.0.1"
    bad = {"Fly-Client-IP": "1.2.3.4, 5.6.7.8"}
    assert client_ip(req(bad), cast(Any, rt("Fly-Client-IP", False))) == "10.0.0.1"
