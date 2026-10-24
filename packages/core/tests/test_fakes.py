from pathlib import Path

import pytest
from invoicedesk.config.models import ModelRole
from invoicedesk.contracts.accounting import BillLine, BillRequest
from invoicedesk.contracts.infra import BoardEvent
from invoicedesk.contracts.llm import (
    InlineImageBlock,
    LLMClient,
    LLMRequest,
    SystemBlock,
    TextBlock,
)
from invoicedesk.contracts.screening import InjectionVerdict
from invoicedesk.fakes import (
    FakeAccounting,
    FakeLLM,
    InMemoryEventPublisher,
    InMemoryJobQueue,
    RecordingLLM,
    ReplayLLM,
    ReplayMiss,
)
from invoicedesk.storage import InMemoryStorage, LocalStorage, safe_key

CLEAN = {"verdict": "clean", "categories": ["none"], "spans": []}


def _req(text: str = "hello") -> LLMRequest:
    return LLMRequest(
        role=ModelRole.INJECTION_CLASSIFIER,
        system=(SystemBlock(text="x" * 4000, cache=True),),
        user=(InlineImageBlock(media_type="image/png", data_b64="AA=="), TextBlock(text=text)),
        output_schema="invoicedesk.contracts.screening.InjectionVerdict",
    )


def test_fake_llm_by_role_and_refusal() -> None:
    llm = FakeLLM(by_role={"injection_classifier": CLEAN})
    assert isinstance(llm, LLMClient)
    r = llm.parse(_req(), InjectionVerdict)
    assert r.parsed == CLEAN and not r.refused
    assert r.usage.cache_read_input_tokens == 1000
    assert r.cost_usd > 0
    assert llm.upload_file(b"x", filename="a.png", media_type="image/png").startswith("file_fake_")
    ref = FakeLLM(refuse_roles={"injection_classifier"})
    assert ref.parse(_req(), InjectionVerdict).refused
    with pytest.raises(KeyError):
        FakeLLM().parse(_req(), InjectionVerdict)
    assert FakeLLM(lambda _r: None).parse(_req(), InjectionVerdict).refused
    canned = llm.parse(_req(), InjectionVerdict)
    assert FakeLLM(lambda _r: canned).parse(_req(), InjectionVerdict) is canned


def test_record_then_replay(tmp_path: Path) -> None:
    rec = RecordingLLM(FakeLLM(by_role={"injection_classifier": CLEAN}), tmp_path)
    first = rec.parse(_req(), InjectionVerdict)
    rec.upload_file(b"x", filename="a", media_type="image/png")
    replay = ReplayLLM(tmp_path)
    assert replay.parse(_req(), InjectionVerdict) == first
    with pytest.raises(ReplayMiss):
        replay.parse(_req("other"), InjectionVerdict)
    fb = ReplayLLM(tmp_path, fallback=FakeLLM(by_role={"injection_classifier": CLEAN}))
    assert fb.parse(_req("other"), InjectionVerdict).parsed == CLEAN
    assert replay.upload_file(b"x", filename="a", media_type="image/png").startswith("file_replay_")


def test_fake_accounting_idempotent() -> None:
    acc = FakeAccounting()
    bill = BillRequest(
        vendor_ref="1",
        doc_number="INV-1",
        txn_date="2026-10-10",  # type: ignore[arg-type]
        lines=(BillLine(description="x", amount_cents=100, expense_account_ref="7"),),
        total_cents=100,
    )
    a = acc.create_bill(bill, idempotency_key="k1")
    b = acc.create_bill(bill, idempotency_key="k1")
    assert a.status == "created" and b.status == "already_exists"
    assert a.external_id == b.external_id and len(acc.bills) == 1
    att = acc.attach_file(
        a.external_id,
        filename="f.pdf",
        content_type="application/pdf",
        data=b"%PDF",
        idempotency_key="k2",
    )
    assert (
        acc.attach_file(
            a.external_id,
            filename="f.pdf",
            content_type="application/pdf",
            data=b"%PDF",
            idempotency_key="k2",
        ).status
        == "already_exists"
    )
    assert att.status == "created"
    with pytest.raises(ValueError):
        acc.create_bill(bill, idempotency_key="")
    with pytest.raises(KeyError):
        acc.attach_file("nope", filename="f", content_type="x", data=b"", idempotency_key="k3")
    for forbidden in ("create_bill_payment", "update_vendor", "set_bank_details"):
        assert not hasattr(acc, forbidden)


def test_events_and_queue() -> None:
    pub = InMemoryEventPublisher()
    seen: list[BoardEvent] = []
    pub.subscribe(seen.append)
    pub.publish(BoardEvent(type="batch.cost_updated", tenant_id="t", batch_cost_usd="0.27"))
    assert len(pub.events) == len(seen) == 1

    q = InMemoryJobQueue()
    j1 = q.enqueue("process_document", document_id="d1", dedupe_key="d1")
    assert q.enqueue("process_document", document_id="d1", dedupe_key="d1") == j1
    q.enqueue("post_bill", document_id="d2", max_attempts=1)
    assert q.claim("w", kinds=["nothing"]) is None
    job = q.claim("w")
    assert job is not None and job.id == j1 and job.attempts == 1
    q.fail(job.id, "boom", retry_in_s=0)
    again = q.claim("w", kinds=["process_document"])
    assert again is not None and again.attempts == 2
    q.complete(again.id)
    j2 = q.claim("w")
    assert j2 is not None
    q.fail(j2.id, "boom")
    assert q.jobs[j2.id]["status"] == "failed"
    assert q.claim("w") is None


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../b", "", "dir/"])
def test_safe_key(bad: str) -> None:
    with pytest.raises(ValueError):
        safe_key(bad)


def test_storages(tmp_path: Path) -> None:
    for st in (
        InMemoryStorage(),
        LocalStorage(tmp_path, signing_secret="s", base_url="http://api"),
    ):
        obj = st.put("maple_row/originals/d1/a.pdf", b"%PDF-1", content_type="application/pdf")
        assert obj.size == 6 and len(obj.sha256) == 64
        assert st.get("maple_row/originals/d1/a.pdf") == b"%PDF-1"
        assert st.exists("maple_row/originals/d1/a.pdf")
        assert st.list("maple_row/") == ["maple_row/originals/d1/a.pdf"]
        assert st.signed_url("maple_row/originals/d1/a.pdf")
        st.delete("maple_row/originals/d1/a.pdf")
        assert not st.exists("maple_row/originals/d1/a.pdf")
    ls = LocalStorage(tmp_path, signing_secret="s")
    ls.put("k/a.png", b"x", content_type="image/png")
    assert ls.content_type("k/a.png") == "image/png"
    assert ls.content_type("k/none.png") == "application/octet-stream"
    url = ls.signed_url("k/a.png")
    exp = int(url.split("exp=")[1].split("&")[0])
    sig = url.split("sig=")[1]
    assert ls.verify("k/a.png", exp, sig)
    assert not ls.verify("k/a.png", exp, "0" * 64)
    assert not ls.verify("k/a.png", 1, ls.sign("k/a.png", 1))
