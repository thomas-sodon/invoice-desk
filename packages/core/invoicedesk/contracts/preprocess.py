"""Preprocessor contract (DESIGN §4.1 Preprocessor, M3).

Coordinates: every box is ``(x0, y0, x1, y1)`` integer pixels in the *processed* page image,
which is resized so its long edge is :data:`TARGET_LONG_EDGE` px. That is the image the model
sees, so model-returned coordinates map 1:1 onto OCR boxes (CLAUDE.md "Images").
Word IDs are global across pages, assigned in reading order starting at 0.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ALLOWED_MIME",
    "MAX_BYTES",
    "MAX_PAGES",
    "TARGET_LONG_EDGE",
    "Box",
    "OcrEngine",
    "OcrWord",
    "PageImage",
    "PreprocessedDocument",
    "Preprocessor",
    "RejectedInput",
    "SanitizeReport",
    "TextLayerSpan",
]

TARGET_LONG_EDGE = 1600
API_MAX_LONG_EDGE = 2576
MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 5
ALLOWED_MIME: frozenset[str] = frozenset(
    {"application/pdf", "image/jpeg", "image/png", "image/heic", "image/heif"}
)

Box = tuple[int, int, int, int]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RejectedInput(ValueError):
    """File type/size/page-count not allowed, or unreadable. Message is safe to show users."""


class OcrWord(_M):
    id: int
    page: int
    text: str
    box: Box
    conf: float = Field(ge=0.0, le=1.0)
    #: Which pass produced it: normal OCR, contrast-enhanced (faint-text) OCR or PDF text layer.
    origin: Literal["ocr", "ocr_enhanced", "pdf_text"] = "ocr"


class TextLayerSpan(_M):
    """A word from a native PDF's text layer with styling (used by the hidden-text detector)."""

    page: int
    text: str
    box: Box  # processed-image px; may lie outside the page when off_page is True
    font_size_pt: float
    fill_rgb: tuple[int, int, int]
    background_rgb: tuple[int, int, int] | None = None  # sampled from the render under the box
    off_page: bool = False


class PageImage(_M):
    index: int
    width: int
    height: int
    storage_key: str  # PNG of the processed page in Storage
    sha256: str
    scale: float = Field(description="processed px per source px (or per PDF pt * dpi/72)")


class SanitizeReport(_M):
    removed_javascript: bool = False
    removed_embedded_files: bool = False
    removed_forms: bool = False
    exif_stripped: bool = False
    converted_from: str | None = None  # e.g. "image/heic"
    notes: list[str] = Field(default_factory=list)


class PreprocessedDocument(_M):
    document_id: str
    sha256: str  # of the original upload
    mime: str
    is_native_pdf: bool  # has a usable text layer
    pages: list[PageImage]
    words: list[OcrWord]  # normal OCR (and pdf_text words when native) in reading order
    enhanced_words: list[OcrWord] = Field(
        default_factory=list, description="CLAHE contrast-stretched OCR pass (faint-text input)"
    )
    text_layer: list[TextLayerSpan] = Field(default_factory=list)
    phash: str  # hex perceptual hash of page 0 (imagehash.phash)
    sanitize: SanitizeReport = Field(default_factory=SanitizeReport)
    ocr_engine: str = "rapidocr"

    def ocr_text(self) -> str:
        return " ".join(w.text for w in self.words)


@runtime_checkable
class OcrEngine(Protocol):
    name: str

    def recognize(self, png: bytes, *, page: int) -> list[OcrWord]:
        """Word-level OCR on a processed page image. Returned ``id`` values are page-local
        placeholders; the preprocessor renumbers them globally."""
        ...


@runtime_checkable
class Preprocessor(Protocol):
    def preprocess(
        self, data: bytes, *, mime: str, document_id: str, filename: str | None = None
    ) -> PreprocessedDocument:
        """Sanitize, render, resize to 1,600 px, OCR, text layer, pHash; store page PNGs.
        Raises :class:`RejectedInput` for disallowed inputs."""
        ...
