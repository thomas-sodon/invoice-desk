"""Hidden-text detector (DESIGN §5.5 pre-LLM layer; R10; CLAUDE.md invariant 8).

Deterministic. A native PDF's text layer is what a text-based reader (and a naive pipeline)
sees; the render is what a human sees. A text-layer word is **hidden** when any of:

* **off-page**: it lies outside the page (``TextLayerSpan.off_page`` or a box wholly outside);
* **tiny**: its font is below :data:`MIN_VISIBLE_PT` (4 pt);
* **camouflaged**: its fill colour is within CIE ΔE < :data:`MAX_DELTA_E` (5) of the background
  sampled under it (white on white, or black on a black bar);
* **absent from the render**: no OCR word of the rendered page (``doc.words``, origin ``ocr``
  or render-corroborated ``pdf_text``) at that place accounts for it (covered by an image or a
  rectangle, invisible render mode, clipped).

Style reasons always flag. "Absent" on its own is noisier (an OCR engine can miss a whole
line of a legitimate document, e.g. inside a dashed frame), so an absent-only word is excused
as "visible, OCR missed it" when nothing at all was read at its place by the normal pass, its
style is clearly visible (ΔE >= :data:`CLEARLY_VISIBLE_DELTA_E`), and either the CLAHE pass reads
it there or the render shows high-contrast ink in its box (:class:`~.pixels.PageInk`, only when
the screener has Storage). The remaining absent-only words are flagged when, per page, they add
up to more than trivial content (:data:`ABSENT_MIN_WORDS` words or :data:`ABSENT_MIN_ALNUM`
characters) or match instruction/bank keywords.

Words that are excused or below the bar are returned as :attr:`HiddenTextReport.unconfirmed`, and
the classifier still reads them: nothing in the text layer is silently dropped.

Known limit: text placed under an opaque picture with dark pixels can pass the ink check; the
classifier still reads it (as "text-layer text not seen by OCR").
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.preprocess import OcrWord, PreprocessedDocument, TextLayerSpan
from invoicedesk.contracts.screening import InjectionCategory, ScreenResult, ScreenSpan
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
from invoicedesk.guardrails.pixels import PageInk
from invoicedesk.masking import mask_text

__all__ = [
    "ABSENT_MIN_ALNUM",
    "ABSENT_MIN_WORDS",
    "CLEARLY_VISIBLE_DELTA_E",
    "MAX_DELTA_E",
    "MIN_VISIBLE_PT",
    "HiddenTextReport",
    "HiddenTextScreener",
    "find_hidden_text",
]

MIN_VISIBLE_PT = 4.0
MAX_DELTA_E = 5.0
#: Fill/background ΔE above which a word's style is unmistakably visible (dark grey on white is
#: ~80; 5-8 % contrast faint grey is ~5-8).
CLEARLY_VISIBLE_DELTA_E = 30.0
ABSENT_MIN_WORDS = 3
ABSENT_MIN_ALNUM = 12
#: Fuzzy threshold for "the OCR at this place reads the same text" (rapidfuzz 0..100).
_SUPPORT_THRESHOLD = 75.0
#: Stricter threshold used to excuse a low-ΔE word that OCR clearly reads (bad background
#: sample on a visible word); a different hidden word overlaid on visible text never passes.
_VISIBLE_DESPITE_COLOUR = 90.0


@dataclass
class HiddenTextReport:
    flagged: list[Group] = field(default_factory=list)
    #: Absent-only words below the triviality bar: not flagged, but passed to the classifier.
    unconfirmed: list[Group] = field(default_factory=list)
    categories: list[InjectionCategory] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(g.text for g in self.flagged)


def _off_page(s: TextLayerSpan, doc: PreprocessedDocument) -> bool:
    if s.off_page:
        return True
    page = next((p for p in doc.pages if p.index == s.page), None)
    if page is None:
        return True  # a text-layer word on a page that was never rendered is not visible
    x0, y0, x1, y1 = s.box
    return x1 <= 0 or y1 <= 0 or x0 >= page.width or y0 >= page.height


def _style_reasons(s: TextLayerSpan, doc: PreprocessedDocument) -> tuple[list[str], bool]:
    """Returns (reasons, colour_only_reason_present)."""
    reasons: list[str] = []
    colour = False
    if _off_page(s, doc):
        reasons.append("off-page")
    if s.font_size_pt < MIN_VISIBLE_PT:
        reasons.append(f"font {s.font_size_pt:.1f}pt")
    if s.background_rgb is not None:
        de = delta_e76(s.fill_rgb, s.background_rgb)
        if de < MAX_DELTA_E:
            colour = True
            reasons.append(f"fill #{_hex(s.fill_rgb)} on #{_hex(s.background_rgb)} (ΔE {de:.1f})")
    return reasons, colour


def _hex(rgb: tuple[int, int, int]) -> str:
    return "".join(f"{max(0, min(255, c)):02X}" for c in rgb)


def _is_exact_pdf_word(s: TextLayerSpan, words: list[OcrWord]) -> bool:
    return any(
        w.origin == "pdf_text" and w.page == s.page and w.box == s.box and w.text == s.text
        for w in words
    )


def _visible_but_missed(
    s: TextLayerSpan,
    visible_text: str,
    enhanced: list[OcrWord],
    ink: PageInk | None,
) -> bool:
    """A clearly styled word at a place where the normal OCR pass read nothing at all, which
    the CLAHE pass reads or which has high-contrast ink in the render."""
    if visible_text.strip() or s.background_rgb is None:
        return False
    if delta_e76(s.fill_rgb, s.background_rgb) < CLEARLY_VISIBLE_DELTA_E:
        return False
    if text_supported(s.text, words_at(s.box, s.page, enhanced), threshold=_SUPPORT_THRESHOLD):
        return True
    return ink is not None and ink.clearly_visible(s.page, s.box) is True


def find_hidden_text(doc: PreprocessedDocument, *, ink: PageInk | None = None) -> HiddenTextReport:
    """Pure function over a :class:`PreprocessedDocument` (plus optional render evidence)."""
    words_by_page: dict[int, list[OcrWord]] = defaultdict(list)
    for w in doc.words:
        words_by_page[w.page].append(w)
    enhanced_by_page: dict[int, list[OcrWord]] = defaultdict(list)
    for w in doc.enhanced_words:
        enhanced_by_page[w.page].append(w)

    styled: list[Finding] = []
    absent: list[Finding] = []
    excused: list[Finding] = []
    for order, s in enumerate(doc.text_layer):
        if alnum_len(s.text) == 0:
            continue  # punctuation-only glyphs carry no instruction
        page_words = words_by_page.get(s.page, [])
        reasons, colour = _style_reasons(s, doc)
        visible_text = "" if reasons and not colour else words_at(s.box, s.page, page_words)
        exact = _is_exact_pdf_word(s, page_words)
        supported = exact or text_supported(s.text, visible_text, threshold=_SUPPORT_THRESHOLD)

        if (
            colour
            and len(reasons) == 1
            and (exact or text_supported(s.text, visible_text, threshold=_VISIBLE_DESPITE_COLOUR))
        ):
            # OCR plainly reads this very word at this place: the background sample was wrong.
            reasons = []
        if reasons:
            styled.append(Finding(s.page, s.text, s.box, tuple(reasons), order))
        elif not supported:
            f = Finding(s.page, s.text, s.box, ("absent from OCR of the render",), order)
            if _visible_but_missed(s, visible_text, enhanced_by_page.get(s.page, []), ink):
                excused.append(f)
            else:
                absent.append(f)

    flagged = list(styled)
    unconfirmed: list[Finding] = list(excused)
    absent_by_page: dict[int, list[Finding]] = defaultdict(list)
    for f in absent:
        absent_by_page[f.page].append(f)
    for page_findings in absent_by_page.values():
        text = " ".join(f.text for f in page_findings)
        substantive = [f for f in page_findings if alnum_len(f.text) >= 2]
        if (
            len(substantive) >= ABSENT_MIN_WORDS
            or alnum_len(text) >= ABSENT_MIN_ALNUM
            or categorize(text)
        ):
            flagged.extend(page_findings)
        else:
            unconfirmed.extend(page_findings)

    groups = group_findings(flagged)
    report = HiddenTextReport(flagged=groups, unconfirmed=group_findings(unconfirmed))
    report.categories = categorize(report.text) if groups else []
    return report


def to_spans(groups: list[Group], *, source: str) -> list[ScreenSpan]:
    """Masked spans (bank/account numbers to last 4) for storage, UI and audit."""
    return [
        ScreenSpan(
            text=mask_text(g.text) or "",
            reason="; ".join(g.reasons),
            page=g.page,
            box=g.box,
            source=source,  # type: ignore[arg-type]
        )
        for g in groups
    ]


class HiddenTextScreener:
    """:class:`~invoicedesk.contracts.screening.Screener` for hidden PDF text."""

    name = "hidden_text"

    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage

    def screen(self, doc: PreprocessedDocument, *, email_body: str | None) -> ScreenResult:
        report = find_hidden_text(doc, ink=PageInk(self.storage, doc))
        if not report.flagged:
            return ScreenResult(detector="hidden_text", verdict="clean", categories=["none"])
        cats = report.categories
        verdict = "malicious" if cats else "suspicious"
        return ScreenResult(
            detector="hidden_text",
            verdict=verdict,
            categories=cats or ["none"],
            spans=to_spans(report.flagged, source="pdf_text"),
        )
