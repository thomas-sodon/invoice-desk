"""End-to-end screening on generated adversarial and clean fixtures, through the REAL
preprocessor (PyMuPDF render, RapidOCR, CLAHE pass). The classifier is a deterministic
FakeLLM. Marked slow (OCR is ~1 s per page per pass)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from invoicedesk.config.models import ModelRole
from invoicedesk.contracts.llm import TextBlock
from invoicedesk.contracts.screening import ScreeningOutcome
from invoicedesk.fakes.llm import FakeLLM
from invoicedesk.guardrails import build_screeners, find_faint_text, find_hidden_text, run_screening
from invoicedesk.guardrails.pixels import PageInk

pytestmark = pytest.mark.slow

ADVERSARIAL_NAMES = [
    "covered_text_pdf",
    "email_injection",
    "faint_photo",
    "off_page_pdf",
    "two_pt_pdf",
    "white_text_pdf",
]
CLEAN_NAMES = ["clean_pdf", "clean_pdf_2", "clean_photo", "clean_scan"]

EXPECTED_DETECTOR = {
    "white_text_pdf": "hidden_text",
    "two_pt_pdf": "hidden_text",
    "off_page_pdf": "hidden_text",
    "covered_text_pdf": "hidden_text",
    "faint_photo": "faint_text",
    "email_injection": "injection_classifier",
}


@pytest.fixture
def screen(gfx: Any) -> Callable[..., ScreeningOutcome]:
    def run(p: Any, llm: FakeLLM | None = None) -> ScreeningOutcome:
        return _screen(p, llm or FakeLLM(gfx.keyword_classifier))

    return run


@pytest.fixture
def get(gfx: Any, processed: Callable[[Any], Any]) -> Callable[[str], Any]:
    def load(name: str) -> Any:
        factory = gfx.ADVERSARIAL.get(name) or gfx.CLEAN.get(name)
        return processed(factory() if factory else gfx.BANK_LETTER())

    return load


def _screen(p: Any, llm: FakeLLM) -> ScreeningOutcome:
    return run_screening(
        p.doc, build_screeners(llm, storage=p.storage), email_body=p.fixture.email_body
    )


def _result(out: ScreeningOutcome, detector: str):
    return next(r for r in out.results if r.detector == detector)


@pytest.mark.parametrize("name", ADVERSARIAL_NAMES)
def test_adversarial_fixture_is_detected_and_quarantined(name: str, get, screen) -> None:
    out = screen(get(name))
    expected = EXPECTED_DETECTOR[name]
    hit = _result(out, expected)
    assert hit.verdict != "clean", (name, out.model_dump())
    assert out.quarantined, name
    if expected != "injection_classifier":
        # the revealed text is in the spans (masked), so later stages see it flagged
        joined = " ".join(s.text for s in hit.spans).lower()
        assert hit.spans and all(s.page == 0 and s.box is not None for s in hit.spans)
        assert "account" in joined or "ignore" in joined or "validation" in joined
        assert "000555012345" not in joined  # masked to last 4 (invariant 9)
    # the classifier read the revealed text too and labelled it
    cls = _result(out, "injection_classifier")
    assert cls.verdict == "malicious"
    assert {"instruction_to_ai", "approval_override"} & set(cls.categories)


def test_hidden_text_spans_carry_the_style_reason(get, screen) -> None:
    def reasons(name: str) -> str:
        return " ".join(s.reason for s in _result(screen(get(name)), "hidden_text").spans)

    assert "fill #FFFFFF on #FFFFFF" in reasons("white_text_pdf")
    assert "font 2.0pt" in reasons("two_pt_pdf")
    assert "off-page" in reasons("off_page_pdf")
    assert "absent from OCR of the render" in reasons("covered_text_pdf")


def test_white_text_categories_from_hidden_text_itself(get, screen) -> None:
    hidden = _result(screen(get("white_text_pdf")), "hidden_text")
    assert hidden.verdict == "malicious"
    assert {"instruction_to_ai", "bank_or_remit_change", "approval_override"} <= set(
        hidden.categories
    )


def test_hidden_text_is_shown_to_the_classifier_labelled(gfx, get, screen) -> None:
    llm = FakeLLM(gfx.keyword_classifier)
    screen(get("two_pt_pdf"), llm)
    (req,) = llm.calls
    sections = "\n".join(b.text for b in req.user if isinstance(b, TextBlock))
    assert '<hidden_text untrusted="true">' in sections
    assert "font 2.0pt" in sections and "ignore all previous" in sections


@pytest.mark.parametrize("name", CLEAN_NAMES)
def test_clean_fixture_has_no_false_positive(name: str, get, screen) -> None:
    out = screen(get(name))
    assert [r.verdict for r in out.results] == ["clean", "clean", "clean"], out.model_dump()
    assert not out.quarantined
    assert out.categories == set()


def test_framed_remit_line_missed_by_ocr_is_not_hidden(get) -> None:
    """RapidOCR can miss a whole visible line inside a dashed frame. With the render (Storage)
    the detector sees the ink there and does not call it hidden."""
    p = get("clean_pdf")
    assert find_hidden_text(p.doc, ink=PageInk(p.storage, p.doc)).flagged == []


def test_faint_text_not_flagged_on_clean_photo_without_storage(get) -> None:
    assert find_faint_text(get("clean_photo").doc).flagged == []


def test_legitimate_bank_change_letter_is_held_not_quarantined(get, screen) -> None:
    out = screen(get("bank_letter"))
    assert _result(out, "hidden_text").verdict == "clean"
    assert _result(out, "faint_text").verdict == "clean"
    cls = _result(out, "injection_classifier")
    assert cls.verdict == "suspicious"
    assert "bank_or_remit_change" in cls.categories
    assert "bank_or_remit_change" in out.categories  # R6 keys on this
    assert not out.quarantined  # R10 does not fire; R6 routes it to two-person approval
    assert cls.spans and all("000555067890" not in s.text for s in cls.spans)


def test_classifier_refusal_on_a_real_document_is_suspicious(gfx, get, screen) -> None:
    llm = FakeLLM(gfx.keyword_classifier, refuse_roles={ModelRole.INJECTION_CLASSIFIER})
    out = screen(get("clean_pdf"), llm)
    cls = _result(out, "injection_classifier")
    assert cls.verdict == "suspicious"
    assert "refus" in cls.spans[0].text
    assert not out.quarantined


def test_injection_recall_and_false_positive_rate(get, screen) -> None:
    """§5.6 targets on these fixtures: recall 100 % (adversarial), FPR 0 (clean)."""
    detected = sum(screen(get(n)).quarantined for n in ADVERSARIAL_NAMES)
    false_pos = sum(screen(get(n)).quarantined for n in CLEAN_NAMES)
    assert detected == len(ADVERSARIAL_NAMES)
    assert false_pos == 0
