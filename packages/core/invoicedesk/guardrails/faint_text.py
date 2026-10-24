"""Faint-text detector for photos and scans (DESIGN §5.5 pre-LLM layer; R10).

Deterministic. The preprocessor runs a second OCR pass on a CLAHE contrast-stretched copy of
each page (``doc.enhanced_words``). Text that only that pass can read (pale grey on white,
printed at a few percent contrast and then photographed) is invisible to a person skimming
the page but perfectly legible to a model, which is how cross-modal injections hide in
images. This detector diffs the enhanced pass against the normal OCR (``doc.words``, any
origin) and flags the recovered text when it is non-trivial: at least
:data:`MIN_RECOVERED_WORDS` words and :data:`MIN_RECOVERED_ALNUM` characters, or any
instruction/bank keyword.

A recovered word is *not* faint when the render shows high-contrast ink in its box (handwriting
the normal pass could not read; needs Storage, see :class:`~.pixels.PageInk`) or when a clearly
styled text-layer word at that place accounts for it (a native PDF line the normal pass missed).

Recovered fragments below that bar (a re-read of a smudged digit, sensor noise read as a
letter) and excused words are returned as :attr:`FaintTextReport.unconfirmed` so the classifier
still sees them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.preprocess import OcrWord, PreprocessedDocument, TextLayerSpan
from invoicedesk.contracts.screening import InjectionCategory, ScreenResult
from invoicedesk.guardrails._common import (
    Finding,
    Group,
    alnum_len,
    categorize,
    delta_e76,
    group_findings,
    text_supported,
    words_at,
)
from invoicedesk.guardrails.hidden_text import CLEARLY_VISIBLE_DELTA_E, MIN_VISIBLE_PT, to_spans
from invoicedesk.guardrails.pixels import PageInk

__all__ = [
    "MIN_RECOVERED_ALNUM",
    "MIN_RECOVERED_CONF",
    "MIN_RECOVERED_WORDS",
    "FaintTextReport",
    "FaintTextScreener",
    "find_faint_text",
]

MIN_RECOVERED_WORDS = 4
MIN_RECOVERED_ALNUM = 12
#: Enhanced-pass words below this OCR confidence are treated as noise.
MIN_RECOVERED_CONF = 0.5
#: Two OCR passes over the same glyphs disagree more than OCR vs a text layer does.
_SUPPORT_THRESHOLD = 60.0


@dataclass
class FaintTextReport:
    flagged: list[Group] = field(default_factory=list)
    unconfirmed: list[Group] = field(default_factory=list)
    categories: list[InjectionCategory] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(g.text for g in self.flagged)


def _covered(w: OcrWord, page_words: list[OcrWord]) -> bool:
    visible = words_at(w.box, w.page, page_words, min_overlap=0.25)
    return text_supported(w.text, visible, threshold=_SUPPORT_THRESHOLD)


def _clear_layer_words(doc: PreprocessedDocument) -> dict[int, list[OcrWord]]:
    """Text-layer words whose style is unmistakably visible, as OcrWords for overlap tests."""
    out: dict[int, list[OcrWord]] = defaultdict(list)
    for i, s in enumerate(doc.text_layer):
        if _clearly_styled(s):
            out[s.page].append(
                OcrWord(id=i, page=s.page, text=s.text, box=s.box, conf=1.0, origin="pdf_text")
            )
    return out


def _clearly_styled(s: TextLayerSpan) -> bool:
    return (
        not s.off_page
        and s.font_size_pt >= MIN_VISIBLE_PT
        and s.background_rgb is not None
        and delta_e76(s.fill_rgb, s.background_rgb) >= CLEARLY_VISIBLE_DELTA_E
    )


def find_faint_text(doc: PreprocessedDocument, *, ink: PageInk | None = None) -> FaintTextReport:
    """Pure function over a :class:`PreprocessedDocument` (plus optional render evidence)."""
    words_by_page: dict[int, list[OcrWord]] = defaultdict(list)
    for w in doc.words:
        words_by_page[w.page].append(w)
    layer_by_page = _clear_layer_words(doc)

    recovered: list[Finding] = []
    noise: list[Finding] = []
    for order, w in enumerate(doc.enhanced_words):
        if alnum_len(w.text) == 0 or _covered(w, words_by_page.get(w.page, [])):
            continue
        f = Finding(w.page, w.text, w.box, ("only legible after contrast enhancement",), order, {})
        excused = _covered(w, layer_by_page.get(w.page, [])) or (
            ink is not None and ink.clearly_visible(w.page, w.box) is True
        )
        (recovered if w.conf >= MIN_RECOVERED_CONF and not excused else noise).append(f)

    text = " ".join(f.text for f in recovered)
    substantive = [f for f in recovered if alnum_len(f.text) >= 2]
    nontrivial = (
        len(substantive) >= MIN_RECOVERED_WORDS and alnum_len(text) >= MIN_RECOVERED_ALNUM
    ) or bool(categorize(text))
    if not nontrivial:
        return FaintTextReport(unconfirmed=group_findings([*recovered, *noise]))
    groups = group_findings(recovered)
    report = FaintTextReport(flagged=groups, unconfirmed=group_findings(noise))
    report.categories = categorize(report.text)
    return report


class FaintTextScreener:
    """:class:`~invoicedesk.contracts.screening.Screener` for contrast-recovered text."""

    name = "faint_text"

    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage

    def screen(self, doc: PreprocessedDocument, *, email_body: str | None) -> ScreenResult:
        report = find_faint_text(doc, ink=PageInk(self.storage, doc))
        if not report.flagged:
            return ScreenResult(detector="faint_text", verdict="clean", categories=["none"])
        cats = report.categories
        return ScreenResult(
            detector="faint_text",
            verdict="malicious" if cats else "suspicious",
            categories=cats or ["none"],
            spans=to_spans(report.flagged, source="ocr_enhanced"),
        )
