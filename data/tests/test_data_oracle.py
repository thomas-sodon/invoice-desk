"""OracleLLM: LLMClient protocol, schema-valid outputs, deterministic, escalation exercised."""

from __future__ import annotations

import base64
import json

import pytest
from invoicedesk.config.models import HAIKU, OPUS, SONNET, ModelRole
from invoicedesk.contracts.extraction import REQUIRED_FIELDS, InvoiceExtraction, field_value
from invoicedesk.contracts.llm import (
    FileImageBlock,
    InlineImageBlock,
    LLMCallError,
    LLMClient,
    LLMRequest,
    SystemBlock,
    TextBlock,
)
from invoicedesk.contracts.screening import InjectionVerdict
from invoicedesk.fakes.llm import RecordingLLM, ReplayLLM
from invoicedesk_data.adversarial import INSTRUCTIONS
from invoicedesk_data.demo import demo_files
from invoicedesk_data.gold import GoldRecord, load_gold
from invoicedesk_data.oracle import OracleLLM, OracleNoise, classify_text
from pydantic import BaseModel

SYSTEM = (SystemBlock(text="You read invoices. Output the schema.", cache=True),)


@pytest.fixture(scope="module")
def demo_gold() -> list[GoldRecord]:
    return load_gold("demo")


@pytest.fixture(scope="module")
def core_gold() -> list[GoldRecord]:
    return load_gold("synthetic-core", split="dev")


def _words_text(rec: GoldRecord) -> str:
    """OCR-like word listing ("id: text") built from the record's gold box texts."""
    lines, i = [], 0
    for name in sorted(rec.boxes):
        for tok in rec.boxes[name].text.split():
            lines.append(f"{i}: {tok}")
            i += 1
    return "\n".join(lines)


def _req(role: ModelRole, *blocks, **meta) -> LLMRequest:
    return LLMRequest(
        role=role,
        system=SYSTEM,
        user=tuple(blocks),
        output_schema="InvoiceExtraction",
        metadata=meta,
    )


def test_satisfies_llmclient_protocol() -> None:
    assert isinstance(OracleLLM(), LLMClient)


def test_extraction_by_uploaded_original_and_page_hash(demo_gold) -> None:
    o = OracleLLM(demo_gold, noise=OracleNoise.none())
    files = {f.gold_id: f for f in demo_files()}
    rec = next(r for r in demo_gold if r.medium == "native" and not r.truth.hidden)
    fid = o.upload_file(files[rec.id].data, filename=rec.filename, media_type=rec.mime)
    resp = o.parse(_req(ModelRole.EXTRACT_T1, FileImageBlock(file_id=fid)), InvoiceExtraction)
    assert resp.stop_reason == "end_turn" and not resp.refused
    ex = InvoiceExtraction.model_validate(resp.parsed)
    gold = rec.invoice_extraction()
    for name in REQUIRED_FIELDS:
        assert field_value(ex, name) == field_value(gold, name), name
    assert resp.model_id == HAIKU
    assert resp.cost_usd > 0 and resp.usage.input_tokens > 0
    # the processed page PNG hash (what the extractor uploads) is recognized too
    assert rec.page_sha256
    o2 = OracleLLM(demo_gold)
    o2._files["file_x"] = rec.page_sha256[0]
    assert o2._match(_req(ModelRole.EXTRACT_T1, FileImageBlock(file_id="file_x")), "") == rec


def test_extraction_by_inline_image(demo_gold) -> None:
    rec = demo_gold[0]
    data = next(f for f in demo_files() if f.gold_id == rec.id).data
    o = OracleLLM(demo_gold, noise=OracleNoise.none())
    blk = InlineImageBlock(media_type="image/png", data_b64=base64.b64encode(data).decode())
    resp = o.parse(_req(ModelRole.EXTRACT_T2, blk), InvoiceExtraction)
    assert InvoiceExtraction.model_validate(resp.parsed).invoice_number.value == (
        rec.truth.invoice_number
    )
    assert resp.model_id == SONNET


def test_extraction_by_ocr_text_with_word_refs(core_gold) -> None:
    o = OracleLLM(core_gold, noise=OracleNoise.none())
    for rec in core_gold[:15]:
        text = _words_text(rec)
        resp = o.parse(_req(ModelRole.ADJUDICATE_T3, TextBlock(text=text)), InvoiceExtraction)
        ex = InvoiceExtraction.model_validate(resp.parsed)
        assert ex.invoice_number.value == rec.truth.invoice_number, rec.id
        assert resp.model_id == OPUS
        # word ids point at the words that spell the value
        words = dict(line.split(": ", 1) for line in text.splitlines())
        ids = ex.total.source.word_ids
        if rec.truth.total_cents is not None and "total" not in rec.truth.illegible_fields:
            assert ids, rec.id
            assert " ".join(words[str(i)] for i in ids) == rec.boxes["total"].text


def test_outputs_are_deterministic(core_gold) -> None:
    a = OracleLLM(core_gold)
    b = OracleLLM(core_gold)
    for rec in core_gold[:10]:
        req = _req(ModelRole.EXTRACT_T1, TextBlock(text=_words_text(rec)))
        ra, rb = a.parse(req, InvoiceExtraction), b.parse(req, InvoiceExtraction)
        assert ra.parsed == rb.parsed
        assert ra.cost_usd == rb.cost_usd and ra.latency_ms == rb.latency_ms


def test_noise_exercises_escalation(core_gold) -> None:
    """Haiku is less sure (and sometimes wrong) on photos/handwriting; Opus is right."""
    o = OracleLLM(core_gold)
    hard = [r for r in core_gold if r.medium in ("photo", "handwritten")]
    low_t1 = wrong_t1 = wrong_t3 = 0
    for rec in hard:
        text = TextBlock(text=_words_text(rec))
        t1 = InvoiceExtraction.model_validate(
            o.parse(_req(ModelRole.EXTRACT_T1, text), InvoiceExtraction).parsed
        )
        t3 = InvoiceExtraction.model_validate(
            o.parse(_req(ModelRole.ADJUDICATE_T3, text), InvoiceExtraction).parsed
        )
        gold = rec.invoice_extraction()
        low_t1 += any(getattr(t1, n).confidence < 0.9 for n in REQUIRED_FIELDS)
        for n in REQUIRED_FIELDS:
            if n in rec.truth.illegible_fields:
                continue
            wrong_t1 += field_value(t1, n) != field_value(gold, n)
            wrong_t3 += field_value(t3, n) != field_value(gold, n)
            if field_value(t1, n) != field_value(gold, n):
                assert getattr(t1, n).confidence < 0.75  # misreads come with low confidence
    assert low_t1 >= len(hard) // 2
    assert wrong_t1 > 0
    assert wrong_t3 == 0


def test_illegible_fields_come_back_empty(core_gold) -> None:
    o = OracleLLM(core_gold, noise=OracleNoise.none())
    rec = (
        next(r for r in core_gold if r.truth.illegible_fields)
        if any(r.truth.illegible_fields for r in core_gold)
        else None
    )
    recs = [rec] if rec else [r for r in load_gold("demo") if r.truth.illegible_fields][:1]
    o = OracleLLM(recs, noise=OracleNoise.none())
    r = recs[0]
    resp = o.parse(_req(ModelRole.EXTRACT_T2, TextBlock(text=_words_text(r))), InvoiceExtraction)
    ex = InvoiceExtraction.model_validate(resp.parsed)
    assert ex.total.value_cents is None and ex.total.confidence < 0.3
    assert ex.handwriting_present
    assert ex.legibility_issues


def test_unknown_document_policies() -> None:
    req = _req(ModelRole.EXTRACT_T1, TextBlock(text="0: nothing\n1: matches"))
    empty = OracleLLM().parse(req, InvoiceExtraction)
    ex = InvoiceExtraction.model_validate(empty.parsed)
    assert ex.doc_type == "other" and ex.total.value_cents is None
    assert all(getattr(ex, n).confidence <= 0.1 for n in REQUIRED_FIELDS)
    assert OracleLLM(unknown="refuse").parse(req, InvoiceExtraction).refused
    with pytest.raises(LLMCallError):
        OracleLLM(unknown="error").parse(req, InvoiceExtraction)


def test_refuse_roles_and_metadata_hint(demo_gold) -> None:
    o = OracleLLM(demo_gold, refuse_roles={ModelRole.EXTRACT_T1})
    resp = o.parse(_req(ModelRole.EXTRACT_T1, TextBlock(text="x")), InvoiceExtraction)
    assert resp.refused and resp.parsed is None
    rec = demo_gold[3]
    resp = o.parse(
        _req(ModelRole.EXTRACT_T2, TextBlock(text="?"), gold_id=rec.id), InvoiceExtraction
    )
    assert InvoiceExtraction.model_validate(resp.parsed).invoice_number.value == (
        rec.truth.invoice_number
    )


def test_hidden_instructions_only_when_the_reader_saw_them(demo_gold) -> None:
    rec = next(r for r in demo_gold if any(h.style == "white" for h in r.truth.hidden))
    o = OracleLLM([rec], noise=OracleNoise.none())
    seen = o.parse(
        _req(
            ModelRole.EXTRACT_T1,
            TextBlock(text=_words_text(rec) + "\n" + rec.truth.hidden[0].text),
            gold_id=rec.id,
        ),
        InvoiceExtraction,
    )
    unseen = o.parse(
        _req(ModelRole.EXTRACT_T1, TextBlock(text=_words_text(rec)), gold_id=rec.id),
        InvoiceExtraction,
    )
    assert seen.parsed and seen.parsed["embedded_instructions"] == [rec.truth.hidden[0].text]
    assert unseen.parsed and unseen.parsed["embedded_instructions"] == []


def test_classifier_clean_on_every_clean_gold_document() -> None:
    for rec in [*load_gold("demo"), *load_gold("synthetic-core", split="dev")]:
        if rec.truth.hidden or rec.email or rec.truth.doc_type != "invoice":
            continue
        text = " ".join(b.text for b in rec.boxes.values())
        text += " Please make checks payable to the vendor. Thank you for your business."
        v = classify_text(text)
        assert v.verdict == "clean", (rec.id, v)


@pytest.mark.parametrize(("text", "cats"), INSTRUCTIONS)
def test_classifier_flags_every_adversarial_instruction(text: str, cats: tuple[str, ...]) -> None:
    v = classify_text(text)
    assert v.verdict == "malicious"
    assert set(cats) <= set(v.categories) | {"urgency_pressure"}


def test_classifier_on_email_bodies_via_parse() -> None:
    o = OracleLLM()
    for rec in load_gold("adversarial"):
        if rec.email is None:
            continue
        req = LLMRequest(
            role=ModelRole.INJECTION_CLASSIFIER,
            system=SYSTEM,
            user=(TextBlock(text=rec.email.body_text),),
            output_schema="InjectionVerdict",
        )
        resp = o.parse(req, InjectionVerdict)
        v = InjectionVerdict.model_validate(resp.parsed)
        assert set(rec.expected.categories) <= set(v.categories), (rec.id, v)
        assert (v.verdict == "malicious") == rec.expected.quarantined, (rec.id, v)
        assert resp.model_id == HAIKU
    mail = next(r for r in load_gold("demo") if r.email)
    assert mail.email is not None
    v = classify_text(mail.email.body_text)
    assert v.verdict == "suspicious" and "bank_or_remit_change" in v.categories


def test_bank_change_letter_is_a_bank_change() -> None:
    rec = next(r for r in load_gold("adversarial") if r.truth.doc_type == "bank_change_letter")
    v = classify_text(" ".join(rec.truth.letter_body))
    assert "bank_or_remit_change" in v.categories


def test_generic_roles_get_a_schema_valid_answer() -> None:
    class Draft(BaseModel):
        subject: str
        body: str
        issues: list[str]
        urgent: bool

    req = LLMRequest(
        role=ModelRole.WORDING,
        system=SYSTEM,
        user=(TextBlock(text="Rapid Rooter invoice is $340.00 over the estimate."),),
        output_schema="Draft",
    )
    resp = OracleLLM().parse(req, Draft)
    d = Draft.model_validate(resp.parsed)
    assert "$340.00" in d.body and d.issues == []


def test_record_and_replay_round_trip(tmp_path, demo_gold) -> None:
    rec = demo_gold[0]
    req = _req(ModelRole.EXTRACT_T1, TextBlock(text=_words_text(rec)))
    live = RecordingLLM(OracleLLM(demo_gold), tmp_path).parse(req, InvoiceExtraction)
    replayed = ReplayLLM(tmp_path).parse(req, InvoiceExtraction)
    assert replayed.parsed == live.parsed
    assert json.loads((tmp_path / f"{req.replay_key()}.json").read_text())["response"]


def test_word_refs_with_the_extractors_own_ocr_listing(core_gold) -> None:
    """The extraction component lists words as ``[i] "text" (x0,y0,x1,y1)``."""
    from invoicedesk.contracts.preprocess import OcrWord, PageImage, PreprocessedDocument
    from invoicedesk.extract.userturn import ocr_words_text

    rec = next(r for r in core_gold if r.medium == "native")
    words: list[OcrWord] = []
    for name in sorted(rec.boxes):
        b = rec.boxes[name]
        for tok in b.text.split():
            words.append(OcrWord(id=len(words), page=0, text=tok, box=b.box, conf=0.99))
    doc = PreprocessedDocument(
        document_id="d1",
        sha256="0" * 64,
        mime="application/pdf",
        is_native_pdf=True,
        pages=[
            PageImage(index=0, width=1237, height=1600, storage_key="k", sha256="0" * 64, scale=1.0)
        ],
        words=words,
        phash="0" * 16,
    )
    listing = ocr_words_text(doc)
    o = OracleLLM(core_gold, noise=OracleNoise.none())
    resp = o.parse(_req(ModelRole.EXTRACT_T1, TextBlock(text=listing)), InvoiceExtraction)
    ex = InvoiceExtraction.model_validate(resp.parsed)
    assert ex.invoice_number.value == rec.truth.invoice_number
    ids = ex.invoice_number.source.word_ids
    assert ids and " ".join(words[i].text for i in ids) == rec.boxes["invoice_number"].text
    assert ex.total.source.word_ids
