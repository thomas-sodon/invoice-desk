"""Preprocessor (M3). STUB: owned by the `preprocess` component, which replaces this file."""

from __future__ import annotations

from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.preprocess import OcrEngine, Preprocessor


def build_preprocessor(storage: Storage, *, ocr: OcrEngine | None = None) -> Preprocessor:
    """Return the default Preprocessor (PyMuPDF + Pillow + RapidOCR/docTR + imagehash)."""
    raise NotImplementedError("preprocess component not built yet")
