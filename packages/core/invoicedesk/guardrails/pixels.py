"""Ink measurements on the processed page renders (optional evidence for the detectors).

The preprocessor stores each processed page PNG in Storage (``PageImage.storage_key``). When a
screener has Storage it can ask "is there clearly visible ink inside this box?":

* hidden-text: a text-layer word that no OCR pass read, but whose box holds high-contrast ink,
  is visible text the OCR engine missed (a dashed frame confuses line detection), not hidden;
  a word covered by a white rectangle or rendered invisibly has no ink there and stays hidden;
* faint-text: a word only the CLAHE pass read, but drawn in high-contrast ink (handwriting the
  normal pass could not read), is not faint.

Without Storage (or when a page cannot be loaded) every query returns ``None`` and the
detectors fall back to their text-only logic, which can only flag more, never less.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.preprocess import Box, PreprocessedDocument

__all__ = ["VISIBLE_INK_CONTRAST", "PageInk"]

#: Luma deviation (0..255) of the strongest ink in a box from the local background above which
#: text is "clearly visible". Measured on generated fixtures: 5-8 %-contrast faint text under
#: simulated photo noise peaks well below this; printed and handwritten ink sits far above it.
VISIBLE_INK_CONTRAST = 60.0
_PAD = 4
_MAX_CACHED_PAGES = 8


class PageInk:
    """Lazily loads page renders for one document and measures ink contrast in boxes."""

    def __init__(self, storage: Storage | None, doc: PreprocessedDocument) -> None:
        self._storage = storage
        self._keys = {p.index: p.storage_key for p in doc.pages}
        self._cache: dict[int, np.ndarray | None] = {}

    def _gray(self, page: int) -> np.ndarray | None:
        if page in self._cache:
            return self._cache[page]
        arr: np.ndarray | None = None
        key = self._keys.get(page)
        if self._storage is not None and key is not None:
            try:
                img = Image.open(io.BytesIO(self._storage.get(key)))
                arr = np.asarray(img.convert("L"), dtype=np.float32)
            except Exception:  # missing/corrupt render: fall back to the text-only logic
                arr = None
        if len(self._cache) >= _MAX_CACHED_PAGES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[page] = arr
        return arr

    def contrast(self, page: int, box: Box) -> float | None:
        """Strongest ink deviation from the local background inside ``box`` (luma 0..255), or
        ``None`` when the render is unavailable or the box is off the image."""
        g = self._gray(page)
        if g is None:
            return None
        h, w = g.shape
        x0, y0, x1, y1 = box
        ix0, iy0, ix1, iy1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        if ix1 - ix0 < 2 or iy1 - iy0 < 2:
            return None
        inner = g[iy0:iy1, ix0:ix1]
        outer = g[max(0, y0 - _PAD) : min(h, y1 + _PAD), max(0, x0 - _PAD) : min(w, x1 + _PAD)]
        bg = float(np.median(outer))
        lo, hi = np.percentile(inner, [3, 97])
        return max(bg - float(lo), float(hi) - bg)

    def clearly_visible(self, page: int, box: Box) -> bool | None:
        c = self.contrast(page, box)
        return None if c is None else c >= VISIBLE_INK_CONTRAST
