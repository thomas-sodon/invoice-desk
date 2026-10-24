"""The default :class:`~invoicedesk.contracts.preprocess.Preprocessor` (DESIGN §4.1, M3).

Steps, in order:

1. **Allow-list.** Size (``MAX_BYTES``), declared type (``ALLOWED_MIME`` or a generic
   ``application/octet-stream``) and the *sniffed* type from magic bytes, which is what we trust.
2. **Convert/sanitize.** HEIC/HEIF -> JPEG (pillow-heif). PDFs: strip JavaScript, embedded files,
   forms and annotations (:mod:`.pdf`); at most ``MAX_PAGES`` pages.
3. **Render + normalise.** PDF pages are rendered so the long edge is exactly
   ``TARGET_LONG_EDGE`` (1,600 px). Raster uploads get EXIF orientation applied, every metadata
   block dropped, and are resized (up or down) to the same long edge.
4. **Store** each processed page PNG at ``<prefix>/renders/<document_id>/page-<n>.png`` with
   ``n`` the 0-based page index. ``prefix`` is the per-call ``storage_prefix`` (the pipeline
   passes the tenant id, or ``visitor/<session_id>`` for try-it uploads so the 24h purge sweeps
   it), falling back to the builder's ``key_prefix``.
5. **OCR** each page (normal pass -> ``words``) and its CLAHE contrast-stretched version
   (``enhanced_words``, the faint-text detector's input).
6. **Text layer** (PDFs): every text-layer word with font size, fill colour, ``off_page`` and the
   background colour sampled from the render, as :class:`TextLayerSpan`.
7. **Native PDFs** (at least ``NATIVE_MIN_ALNUM`` alphanumerics of text-layer words
   *corroborated by OCR of the render*, see ``_merge_native``): corroborated text-layer words
   replace those OCR words in ``words`` with ``origin="pdf_text"`` (exact text, exact boxes).
   Uncorroborated text-layer words (white-on-white, 2 pt, off-page, invisible render mode,
   covered) are *not* added to ``words``; they stay in ``text_layer`` only. So ``words`` is
   always "what is visibly on the page", and the hidden-text detector can compare ``text_layer``
   against ``words`` of any origin. OCR words with no text-layer counterpart (stamps,
   handwriting) stay as ``origin="ocr"``.
8. **pHash** of page 0 (``imagehash.phash``, hex).

Word ids: ``words`` are numbered 0..N-1 globally in reading order (page, then line, then x).
``enhanced_words`` continue from N so an id never names two different words.
"""

from __future__ import annotations

import hashlib
import re

import numpy as np
from PIL import Image
from rapidfuzz import fuzz

from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.preprocess import (
    ALLOWED_MIME,
    MAX_BYTES,
    OcrEngine,
    OcrWord,
    PageImage,
    PreprocessedDocument,
    RejectedInput,
    SanitizeReport,
    TextLayerSpan,
)
from invoicedesk.preprocess import images, pdf
from invoicedesk.preprocess.ocr import default_engine, reading_order

__all__ = [
    "MIN_VISIBLE_DELTA_E",
    "MIN_VISIBLE_PT",
    "NATIVE_MIN_ALNUM",
    "DefaultPreprocessor",
    "delta_e76",
    "render_key",
]

NATIVE_MIN_ALNUM = 10
_GENERIC_MIME = {"", "application/octet-stream", "binary/octet-stream"}
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: OCR corroboration: overlap (intersection / smaller box area) and text similarity thresholds.
_OVERLAP_MIN = 0.3
_SIMILARITY_MIN = 60.0
#: A component's joined text-layer text must match its joined OCR text at least this well.
_GROUP_SIMILARITY_MIN = 75.0
#: Text-layer and OCR word heights must agree within this ratio (2 pt text on 12 pt words fails).
_HEIGHT_RATIO_MIN = 0.5
#: DESIGN §5.5 hidden-text thresholds: text below these is never "visible" corroboration.
MIN_VISIBLE_PT = 4.0
MIN_VISIBLE_DELTA_E = 5.0


def render_key(prefix: str, document_id: str, index: int) -> str:
    return f"{prefix.strip('/')}/renders/{document_id}/page-{index}.png"


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    area = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (ix * iy) / max(area, 1)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


class DefaultPreprocessor:
    """PyMuPDF + Pillow + OCR engine + imagehash. Thread-safe; the OCR model is shared."""

    def __init__(
        self,
        storage: Storage,
        *,
        ocr: OcrEngine | None = None,
        key_prefix: str = "default",
        enhanced_pass: bool = True,
    ) -> None:
        self.storage = storage
        self.ocr: OcrEngine = ocr or default_engine()
        self.key_prefix = key_prefix
        self.enhanced_pass = enhanced_pass

    # ------------------------------------------------------------------ public
    def preprocess(  # noqa: PLR0915
        self,
        data: bytes,
        *,
        mime: str,
        document_id: str,
        filename: str | None = None,
        storage_prefix: str | None = None,
    ) -> PreprocessedDocument:
        if not _DOC_ID_RE.match(document_id):
            raise ValueError(f"invalid document_id: {document_id!r}")
        effective = self._check_input(data, mime)
        sha = hashlib.sha256(data).hexdigest()
        report = SanitizeReport()
        if effective != (mime or "").lower() and (mime or "").lower() not in _GENERIC_MIME:
            report.notes.append(f"declared type {mime} but content is {effective}; using content")

        rendered: list[tuple[int, Image.Image, float]] = []
        layer: list[pdf.PdfWord] = []
        if effective == "application/pdf":
            doc, pdf_report = pdf.open_sanitized(data)
            report = pdf_report.model_copy(update={"notes": [*report.notes, *pdf_report.notes]})
            try:
                for page in doc:
                    rp = pdf.render_page(page)
                    rendered.append((rp.index, rp.image, rp.scale))
                    layer.extend(pdf.text_layer_words(page, rp))
            finally:
                pdf.close_quietly(doc)
        else:
            raster = data
            if effective in ("image/heic", "image/heif"):
                raster = images.heic_to_jpeg(data)
                report.converted_from = effective
                report.exif_stripped = True
                effective = "image/jpeg"
            img, had_meta, notes = images.load_raster(raster)
            report.exif_stripped = report.exif_stripped or had_meta
            report.notes.extend(notes)
            img, scale = images.resize_long_edge(img)
            rendered.append((0, img, scale))

        invisible = sum(1 for w in layer if w.invisible)
        if invisible:
            report.notes.append(f"{invisible} text-layer word(s) are invisible (render mode/alpha)")

        prefix = storage_prefix or self.key_prefix
        pages: list[PageImage] = []
        ocr_by_page: dict[int, list[OcrWord]] = {}
        enhanced: list[OcrWord] = []
        for index, img, scale in rendered:
            png = images.encode_png(img)
            key = render_key(prefix, document_id, index)
            stored = self.storage.put(key, png, content_type="image/png")
            pages.append(
                PageImage(
                    index=index,
                    width=img.width,
                    height=img.height,
                    storage_key=stored.key,
                    sha256=hashlib.sha256(png).hexdigest(),
                    scale=scale,
                )
            )
            ocr_by_page[index] = self.ocr.recognize(png, page=index)
            if self.enhanced_pass:
                faint_png = images.clahe_png(np.asarray(img))
                enhanced.extend(
                    w.model_copy(update={"origin": "ocr_enhanced"})
                    for w in self.ocr.recognize(faint_png, page=index)
                )

        spans: list[TextLayerSpan] = [w.span for w in layer]
        merged: list[OcrWord] = []
        for index, _, _ in rendered:
            page_layer = [w for w in layer if w.span.page == index]
            merged.extend(self._merge_native(page_layer, ocr_by_page[index]))
        # "Usable text layer" = text-layer text the render corroborates; hidden-only layers
        # (white, invisible, off-page, or a scan's invisible OCR layer) do not count.
        corroborated = sum(
            sum(c.isalnum() for c in w.text) for w in merged if w.origin == "pdf_text"
        )
        is_native = corroborated >= NATIVE_MIN_ALNUM
        words = merged if is_native else [w for i in sorted(ocr_by_page) for w in ocr_by_page[i]]
        words = [w.model_copy(update={"id": i}) for i, w in enumerate(words)]
        enhanced = [w.model_copy(update={"id": len(words) + i}) for i, w in enumerate(enhanced)]

        return PreprocessedDocument(
            document_id=document_id,
            sha256=sha,
            mime=effective,
            is_native_pdf=is_native,
            pages=pages,
            words=words,
            enhanced_words=enhanced,
            text_layer=spans,
            phash=images.phash_hex(rendered[0][1]),
            sanitize=report,
            ocr_engine=getattr(self.ocr, "name", type(self.ocr).__name__),
        )

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _check_input(data: bytes, mime: str) -> str:
        declared = (mime or "").split(";")[0].strip().lower()
        if not data:
            raise RejectedInput("The file is empty.")
        if len(data) > MAX_BYTES:
            raise RejectedInput(
                f"The file is {len(data) / 1_048_576:.1f} MB; the limit is "
                f"{MAX_BYTES // 1_048_576} MB."
            )
        if declared not in ALLOWED_MIME and declared not in _GENERIC_MIME:
            raise RejectedInput("Only PDF, JPEG, PNG and HEIC files are accepted.")
        sniffed = images.sniff_mime(data)
        if sniffed is None or sniffed not in ALLOWED_MIME:
            raise RejectedInput("Only PDF, JPEG, PNG and HEIC files are accepted.")
        return sniffed

    @staticmethod
    def _merge_native(layer: list[pdf.PdfWord], ocr_words: list[OcrWord]) -> list[OcrWord]:
        """Swap OCR words for OCR-corroborated text-layer words (see module docstring, step 7).

        A text-layer word is a candidate only if it could be visible at all (on-page, not
        invisible, at least ``MIN_VISIBLE_PT`` and fill/background ΔE >= ``MIN_VISIBLE_DELTA_E``,
        the hidden-text detector's thresholds) and it overlaps OCR words of a compatible height
        with similar text. Candidates and OCR words are then grouped into connected components by
        overlap; a component is swapped only when its text-layer words, joined, match its OCR
        words, joined (``fuzz.ratio``). So a short hidden word sitting on a longer visible one
        ("1" on "$1,240.00") can never replace it, and merged/split OCR words still match.
        """
        cands: list[TextLayerSpan] = []
        edges: dict[int, list[int]] = {}
        for pw in layer:
            s = pw.span
            if pw.invisible or not _could_be_visible(s):
                continue
            hits = [
                i
                for i, o in enumerate(ocr_words)
                if _overlap(s.box, o.box) >= _OVERLAP_MIN and _height_ok(s.box, o.box)
            ]
            if not hits:
                continue
            seen = " ".join(ocr_words[i].text for i in hits)
            if fuzz.partial_ratio(_norm(s.text), _norm(seen)) < _SIMILARITY_MIN:
                continue
            edges[len(cands)] = hits
            cands.append(s)

        # Union-find over nodes: candidates 0..C-1, OCR words C..C+O-1.
        n_c = len(cands)
        parent = list(range(n_c + len(ocr_words)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for ci, hits in edges.items():
            for oi in hits:
                parent[find(ci)] = find(n_c + oi)
        groups: dict[int, tuple[list[int], list[int]]] = {}
        for ci in range(n_c):
            groups.setdefault(find(ci), ([], []))[0].append(ci)
        for oi in range(len(ocr_words)):
            root = find(n_c + oi)
            if root in groups:
                groups[root][1].append(oi)

        accepted: list[OcrWord] = []
        covered: set[int] = set()
        for c_idx, o_idx in groups.values():
            layer_text = "".join(
                _norm(cands[i].text) for i in sorted(c_idx, key=lambda i: _order(cands[i].box))
            )
            ocr_text = "".join(
                _norm(ocr_words[i].text)
                for i in sorted(o_idx, key=lambda i: _order(ocr_words[i].box))
            )
            score = fuzz.ratio(layer_text, ocr_text)
            if score < _GROUP_SIMILARITY_MIN:
                continue
            covered.update(o_idx)
            accepted.extend(
                OcrWord(
                    id=0,
                    page=cands[i].page,
                    text=cands[i].text,
                    box=cands[i].box,
                    conf=round(score / 100.0, 4),
                    origin="pdf_text",
                )
                for i in c_idx
            )
        rest = [o for i, o in enumerate(ocr_words) if i not in covered]
        return reading_order([*accepted, *rest])


def _order(box: tuple[int, int, int, int]) -> tuple[int, int]:
    return (box[1] // 8, box[0])


def _height_ok(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ha, hb = max(1, a[3] - a[1]), max(1, b[3] - b[1])
    return min(ha, hb) / max(ha, hb) >= _HEIGHT_RATIO_MIN


def _srgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    def lin(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e76(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """CIE76 colour difference of two sRGB colours."""
    la, lb = _srgb_to_lab(a), _srgb_to_lab(b)
    return sum((p - q) ** 2 for p, q in zip(la, lb, strict=True)) ** 0.5


def _could_be_visible(s: TextLayerSpan) -> bool:
    """False for text the hidden-text detector would call hidden by style (DESIGN §5.5)."""
    if s.off_page or not s.text.strip() or s.font_size_pt < MIN_VISIBLE_PT:
        return False
    return s.background_rgb is None or delta_e76(s.fill_rgb, s.background_rgb) >= (
        MIN_VISIBLE_DELTA_E
    )
