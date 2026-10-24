"""OCR engines behind :class:`invoicedesk.contracts.preprocess.OcrEngine`.

* :class:`RapidOCREngine` (default): RapidOCR on onnxruntime with bundled PP-OCRv4 models. It
  returns *line* quads, so :func:`split_line` turns each line into word boxes: proportional
  character-width estimates first, then snapped to the ink gaps of the rendered line.
* :class:`DocTREngine` (optional ``doctr`` extra): native word boxes from docTR.
* :class:`AzureDIEngine` (optional ``azure`` extra): Azure AI Document Intelligence
  ``prebuilt-read``. Only usable with ``INVOICEDESK_MODE=live`` plus ``AZURE_DI_ENDPOINT`` and
  ``AZURE_DI_KEY``; never called by tests.

All engines return boxes in the pixel space of the PNG they were given (the processed 1,600 px
page) and page-local placeholder ids; the preprocessor renumbers ids globally in reading order.
"""

from __future__ import annotations

import functools
import importlib
import os
import threading
from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np

from invoicedesk.contracts.preprocess import Box, OcrWord

__all__ = [
    "AzureDIEngine",
    "DocTREngine",
    "EngineUnavailable",
    "RapidOCREngine",
    "char_weight",
    "default_engine",
    "reading_order",
    "split_line",
]

Quad = Sequence[Sequence[float]]  # 4 points: tl, tr, br, bl


class EngineUnavailable(RuntimeError):
    """The optional OCR backend is not installed or not configured."""


# --------------------------------------------------------------------------- word splitting

# Approximate Helvetica advance widths (1/1000 em). Only the relative sizes matter.
_NARROW = dict.fromkeys("ijl.,:;|!'`I ", 278) | dict.fromkeys('frt()[]-"/', 333)
_WIDE = dict.fromkeys("mwMW%@", 833) | dict.fromkeys("CDGHNOQU&", 778)


def char_weight(c: str) -> int:
    """Relative advance width of a character (proportional-font approximation)."""
    if c in _NARROW:
        return _NARROW[c]
    if c in _WIDE:
        return _WIDE[c]
    if c.isupper():
        return 667
    return 556


def _interp(p: Sequence[float], q: Sequence[float], t: float) -> tuple[float, float]:
    return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)


def _ink_columns(crop: np.ndarray) -> np.ndarray | None:
    """Boolean per-column 'has ink' for a grayscale line crop, or None if there is no contrast."""
    if crop.size == 0 or int(crop.max()) - int(crop.min()) < 24:
        return None
    _, binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink = binary > 0
    if ink.mean() > 0.5:  # light text on a dark band: the minority class is the ink
        ink = ~ink
    return ink.sum(axis=0) > crop.shape[0] // 40


def _gaps(cols: np.ndarray) -> list[tuple[int, int]]:
    """Runs of ink-free columns as half-open ``(start, end)``, excluding the margins."""
    gaps: list[tuple[int, int]] = []
    n = len(cols)
    i = 0
    while i < n:
        if not cols[i]:
            j = i
            while j < n and not cols[j]:
                j += 1
            if i > 0 and j < n:
                gaps.append((i, j))
            i = j
        else:
            i += 1
    return gaps


#: An ink gap at least this fraction of the line height, inside a token with no space there,
#: is treated as a missing space (RapidOCR sometimes drops spaces: "MapleRowProperty").
_UNSPACED_GAP_FRAC = 0.22


def _snap_to_ink(
    tokens: list[str], fr: list[tuple[float, float]], cols: np.ndarray, *, height: int
) -> list[tuple[str, int, int]]:
    """Word extents (crop-relative columns) for a horizontal line: snap each estimated space to
    the widest nearby ink gap, split tokens at clearly word-sized interior gaps, then tighten
    each word to its ink."""
    width = len(cols)
    first = int(np.argmax(cols))
    last = width - int(np.argmax(cols[::-1]))
    span = max(1, last - first)
    seg = [[first + a * span, first + b * span] for a, b in fr]
    gaps = _gaps(cols)
    tol = 0.6 * height
    used: set[int] = set()
    for k in range(len(seg) - 1):
        est = (seg[k][1] + seg[k + 1][0]) / 2
        best = None
        for gi, (gs, ge) in enumerate(gaps):
            if gi in used or ge < est - tol or gs > est + tol:
                continue
            if best is None or (ge - gs) > (gaps[best][1] - gaps[best][0]):
                best = gi
        if best is not None:
            used.add(best)
            seg[k][1], seg[k + 1][0] = gaps[best]

    widths = sorted(ge - gs for gs, ge in gaps) or [0]
    median_gap = widths[len(widths) // 2]
    min_split = max(_UNSPACED_GAP_FRAC * height, 2.5 * median_gap, 3)
    pieces: list[tuple[str, float, float]] = []
    for tok, (a, b) in zip(tokens, seg, strict=True):
        inner = [
            (gs, ge)
            for gi, (gs, ge) in enumerate(gaps)
            if gi not in used and gs > a + 1 and ge < b - 1 and ge - gs >= min_split
        ]
        if not inner or len(tok) < 2:
            pieces.append((tok, a, b))
            continue
        weights = [char_weight(c) for c in tok]
        total = float(sum(weights))
        cum = np.cumsum(weights) / total  # fraction at the end of each char
        cuts: list[tuple[int, float, float]] = []
        for gs, ge in inner:
            # fraction of the token's ink width left of the gap (the gap itself excluded)
            f = (gs - a) / max(1.0, (b - a) - (ge - gs))
            idx = int(np.argmin(np.abs(cum[:-1] - f))) + 1  # split before char idx
            left, right = tok[idx - 1], tok[idx]
            # Only between alphanumerics, never digit|digit ("$1,240.00", Courier "1100").
            if not (left.isalnum() and right.isalnum()) or (left.isdigit() and right.isdigit()):
                continue
            if all(c[0] != idx for c in cuts):
                cuts.append((idx, gs, ge))
        cuts.sort()
        start_i, start_x = 0, a
        for idx, gs, ge in cuts:
            pieces.append((tok[start_i:idx], start_x, gs))
            start_i, start_x = idx, ge
        pieces.append((tok[start_i:], start_x, b))

    out: list[tuple[str, int, int]] = []
    for tok, a, b in pieces:
        lo, hi = max(0, round(a)), min(width, round(b))
        sub = cols[lo:hi]
        if sub.any():
            hi = lo + len(sub) - int(np.argmax(sub[::-1]))
            lo = lo + int(np.argmax(sub))
        out.append((tok, lo, max(hi, lo + 1)))
    return out


def split_line(
    quad: Quad,
    text: str,
    conf: float,
    *,
    page: int,
    gray: np.ndarray | None = None,
    origin: str = "ocr",
) -> list[OcrWord]:
    """Split one OCR line into word boxes.

    Boundaries are first estimated from character-width proportions along the line's baseline
    direction (so slanted lines work). For near-horizontal lines with an image available, each
    estimated inter-word space is snapped to the nearest ink-free column run, and each word's
    horizontal extent is tightened to its ink.
    """
    tokens = text.split()
    if not tokens:
        return []
    tl, tr, br, bl = quad
    # character spans along the line, in weight units
    starts: list[float] = []
    ends: list[float] = []
    pos = 0.0
    i = 0
    stripped = text.strip()
    for tok in tokens:
        j = stripped.index(tok, i)
        pos += sum(char_weight(c) for c in stripped[i:j])
        starts.append(pos)
        pos += sum(char_weight(c) for c in tok)
        ends.append(pos)
        i = j + len(tok)
    total = pos or 1.0
    fr = [(s / total, e / total) for s, e in zip(starts, ends, strict=True)]

    x0 = min(p[0] for p in quad)
    x1 = max(p[0] for p in quad)
    y0 = min(p[1] for p in quad)
    y1 = max(p[1] for p in quad)
    dx, dy = tr[0] - tl[0], tr[1] - tl[1]
    horizontal = dx > 0 and abs(dy) <= 0.05 * dx

    if horizontal and gray is not None:
        ix0, iy0 = max(0, int(x0)), max(0, int(y0))
        ix1, iy1 = min(gray.shape[1], int(np.ceil(x1))), min(gray.shape[0], int(np.ceil(y1)))
        cols = _ink_columns(gray[iy0:iy1, ix0:ix1]) if ix1 > ix0 and iy1 > iy0 else None
        if cols is not None and cols.any():
            pieces = _snap_to_ink(tokens, fr, cols, height=iy1 - iy0)
            return [
                OcrWord(
                    id=n,
                    page=page,
                    text=tok,
                    box=(ix0 + lo, iy0, ix0 + hi, iy1),
                    conf=_clamp(conf),
                    origin=origin,  # type: ignore[arg-type]
                )
                for n, (tok, lo, hi) in enumerate(pieces)
            ]

    out: list[OcrWord] = []
    for n, (tok, (a, b)) in enumerate(zip(tokens, fr, strict=True)):
        pts = [_interp(tl, tr, a), _interp(tl, tr, b), _interp(bl, br, a), _interp(bl, br, b)]
        box: Box = (
            int(np.floor(min(p[0] for p in pts))),
            int(np.floor(min(p[1] for p in pts))),
            int(np.ceil(max(p[0] for p in pts))),
            int(np.ceil(max(p[1] for p in pts))),
        )
        out.append(
            OcrWord(id=n, page=page, text=tok, box=box, conf=_clamp(conf), origin=origin)  # type: ignore[arg-type]
        )
    return out


def _box(x0: float, y0: float, x1: float, y1: float) -> Box:
    return (int(np.floor(x0)), int(np.floor(y0)), int(np.ceil(x1)), int(np.ceil(y1)))


def _clamp(v: float) -> float:
    return float(min(1.0, max(0.0, v)))


def reading_order(words: Sequence[OcrWord]) -> list[OcrWord]:
    """Sort words top-to-bottom into lines (by vertical overlap), then left-to-right."""
    if not words:
        return []
    by_y = sorted(words, key=lambda w: ((w.box[1] + w.box[3]) / 2, w.box[0]))
    lines: list[list[OcrWord]] = []
    for w in by_y:
        cy = (w.box[1] + w.box[3]) / 2
        h = max(1, w.box[3] - w.box[1])
        if lines:
            cur = lines[-1]
            ly0 = min(x.box[1] for x in cur)
            ly1 = max(x.box[3] for x in cur)
            lcy = (ly0 + ly1) / 2
            lh = max(1, ly1 - ly0)
            if abs(cy - lcy) <= 0.5 * min(h, lh):
                cur.append(w)
                continue
        lines.append([w])
    out: list[OcrWord] = []
    for line in lines:
        out.extend(sorted(line, key=lambda w: (w.box[0], w.box[1])))
    return out


def _decode(png: bytes) -> tuple[np.ndarray, np.ndarray]:
    arr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise ValueError("OCR input is not a decodable image")
    return arr, cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)


# --------------------------------------------------------------------------- RapidOCR


@functools.cache
def _rapidocr_instance() -> Any:
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


class RapidOCREngine:
    """Default engine. The underlying model instance is created once per process and shared;
    calls are serialized because onnxruntime already uses every core per call."""

    name = "rapidocr"
    _lock = threading.Lock()

    def __init__(self, *, min_line_score: float = 0.5) -> None:
        self.min_line_score = min_line_score

    def recognize(self, png: bytes, *, page: int) -> list[OcrWord]:
        bgr, gray = _decode(png)
        engine = _rapidocr_instance()
        with self._lock:
            result, _ = engine(bgr)
        words: list[OcrWord] = []
        for item in result or []:
            quad, text, score = item[0], str(item[1]), float(item[2])
            if score < self.min_line_score or not text.strip():
                continue
            words.extend(split_line(quad, text, score, page=page, gray=gray))
        ordered = reading_order(words)
        return [w.model_copy(update={"id": i}) for i, w in enumerate(ordered)]


@functools.cache
def default_engine() -> RapidOCREngine:
    return RapidOCREngine()


# --------------------------------------------------------------------------- docTR (optional)


@functools.cache
def _doctr_predictor() -> Any:
    try:
        models = importlib.import_module("doctr.models")
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise EngineUnavailable("docTR is not installed (uv sync --extra doctr)") from exc
    return models.ocr_predictor(pretrained=True)


class DocTREngine:
    """docTR word-level OCR (``uv sync --extra doctr``). Import is deferred until first use."""

    name = "doctr"
    _lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        try:
            importlib.import_module("doctr")
        except ImportError:
            return False
        return True

    def recognize(self, png: bytes, *, page: int) -> list[OcrWord]:
        bgr, _ = _decode(png)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        predictor = _doctr_predictor()
        with self._lock:
            result = predictor([rgb])
        words: list[OcrWord] = []
        for block in result.pages[0].blocks:
            for line in block.lines:
                for word in line.words:
                    (ax, ay), (bx, by) = word.geometry
                    words.append(
                        OcrWord(
                            id=0,
                            page=page,
                            text=str(word.value),
                            box=_box(ax * w, ay * h, bx * w, by * h),
                            conf=_clamp(float(word.confidence)),
                        )
                    )
        ordered = reading_order([x for x in words if x.text.strip()])
        return [x.model_copy(update={"id": i}) for i, x in enumerate(ordered)]


# --------------------------------------------------------------------------- Azure DI (optional)


class AzureDIEngine:
    """Azure AI Document Intelligence ``prebuilt-read`` (``uv sync --extra azure``).

    Gated: requires ``INVOICEDESK_MODE=live``, ``AZURE_DI_ENDPOINT`` and ``AZURE_DI_KEY``. It is a
    paid external API, so it is never constructed in tests or offline mode.
    """

    name = "azure_di"

    def __init__(self, *, endpoint: str | None = None, key: str | None = None) -> None:
        if os.environ.get("INVOICEDESK_MODE", "offline") != "live":
            raise EngineUnavailable("Azure DI is only available with INVOICEDESK_MODE=live")
        self.endpoint = endpoint or os.environ.get("AZURE_DI_ENDPOINT", "")
        self.key = key or os.environ.get("AZURE_DI_KEY", "")
        if not self.endpoint or not self.key:
            raise EngineUnavailable("AZURE_DI_ENDPOINT and AZURE_DI_KEY must be set")
        try:
            di = importlib.import_module("azure.ai.documentintelligence")
            cred = importlib.import_module("azure.core.credentials")
        except ImportError as exc:  # pragma: no cover - optional extra
            raise EngineUnavailable("azure-ai-documentintelligence is not installed") from exc
        self._client = di.DocumentIntelligenceClient(
            self.endpoint, cred.AzureKeyCredential(self.key)
        )

    def recognize(self, png: bytes, *, page: int) -> list[OcrWord]:  # pragma: no cover - live
        poller = self._client.begin_analyze_document(
            "prebuilt-read", body=png, content_type="application/octet-stream"
        )
        result = poller.result()
        words: list[OcrWord] = []
        for p in result.pages or []:
            for w in p.words or []:
                poly = list(w.polygon or [])
                xs, ys = poly[0::2], poly[1::2]
                if not xs:
                    continue
                words.append(
                    OcrWord(
                        id=0,
                        page=page,
                        text=w.content,
                        box=_box(min(xs), min(ys), max(xs), max(ys)),
                        conf=_clamp(float(w.confidence or 0.0)),
                    )
                )
        ordered = reading_order(words)
        return [x.model_copy(update={"id": i}) for i, x in enumerate(ordered)]
