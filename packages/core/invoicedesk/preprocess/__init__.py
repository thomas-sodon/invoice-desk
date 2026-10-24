"""Preprocessor (DESIGN §4.1, M3): sanitize, render, resize to 1,600 px, OCR words + boxes,
styled PDF text layer, contrast-enhanced OCR pass and pHash. See :mod:`.pipeline` for the steps
and the storage key layout."""

from __future__ import annotations

from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.preprocess import OcrEngine, Preprocessor
from invoicedesk.preprocess.ocr import (
    AzureDIEngine,
    DocTREngine,
    EngineUnavailable,
    RapidOCREngine,
    default_engine,
)
from invoicedesk.preprocess.pipeline import DefaultPreprocessor, render_key

__all__ = [
    "AzureDIEngine",
    "DefaultPreprocessor",
    "DocTREngine",
    "EngineUnavailable",
    "RapidOCREngine",
    "build_preprocessor",
    "default_engine",
    "render_key",
]


def build_preprocessor(
    storage: Storage,
    *,
    ocr: OcrEngine | None = None,
    key_prefix: str = "default",
    enhanced_pass: bool = True,
) -> Preprocessor:
    """Return the default Preprocessor (PyMuPDF + Pillow + RapidOCR/docTR + imagehash).

    ``ocr`` defaults to the process-wide cached :class:`RapidOCREngine`. ``key_prefix`` is used
    for render keys when a call does not pass ``storage_prefix`` (tenant id or
    ``visitor/<session_id>``).
    """
    return DefaultPreprocessor(storage, ocr=ocr, key_prefix=key_prefix, enhanced_pass=enhanced_pass)
