"""Synthetic handwriting (stand-in for hand-filled carbon-copy books, DESIGN §6.2 step 5).

No handwriting font ships in this environment, so values are written with OpenCV's Hershey
script stroke font, one glyph at a time with per-glyph rotation, baseline wobble, scale and
spacing jitter and varying stroke width, in ballpoint-blue ink. The gold box is the exact
extent of the ink written. ``illegible=True`` writes the value, then over-writes it with
corrected digits, scribbles across it and smears the region: the "deliberately illegible
total" case. This substitution is recorded in docs/GAPS.md.
"""

from __future__ import annotations

import random

import cv2
import numpy as np

__all__ = ["write_text"]

_FONT = cv2.FONT_HERSHEY_SCRIPT_SIMPLEX


def _glyph(ch: str, scale: float, thickness: int) -> tuple[np.ndarray, int]:
    (w, h), base = cv2.getTextSize(ch, _FONT, scale, thickness)
    pad = 4 + thickness
    canvas = np.zeros((h + base + 2 * pad, max(w, 1) + 2 * pad), dtype=np.uint8)
    cv2.putText(canvas, ch, (pad, pad + h), _FONT, scale, 255, thickness, cv2.LINE_AA)
    return canvas, w


def _paste(
    img: np.ndarray, mask: np.ndarray, x: int, y: int, ink: tuple[int, int, int]
) -> tuple[int, int, int, int] | None:
    h, w = mask.shape
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x0 >= x1 or y0 >= y1:
        return None
    m = mask[y0 - y : y1 - y, x0 - x : x1 - x].astype(np.float32) / 255.0
    region = img[y0:y1, x0:x1].astype(np.float32)
    color = np.array(ink, dtype=np.float32)
    region = region * (1 - m[..., None]) + color * m[..., None]
    img[y0:y1, x0:x1] = region.astype(np.uint8)
    ys, xs = np.nonzero(m > 0.25)
    if len(xs) == 0:
        return None
    return (x0 + int(xs.min()), y0 + int(ys.min()), x0 + int(xs.max()) + 1, y0 + int(ys.max()) + 1)


def _union(
    a: tuple[int, int, int, int] | None, b: tuple[int, int, int, int] | None
) -> tuple[int, int, int, int] | None:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def write_text(
    img: np.ndarray,
    text: str,
    box: tuple[int, int, int, int],
    rng: random.Random,
    *,
    font_px: float,
    ink: tuple[int, int, int] = (28, 42, 120),
    illegible: bool = False,
    align_right: bool = False,
) -> tuple[int, int, int, int]:
    """Write ``text`` into ``img`` (RGB, modified in place) inside the layout ``box``.

    Returns the ink bounding box (the gold box)."""
    x0, y0, x1, y1 = box
    # Hershey script cap height is about 22 px at scale 1.0.
    scale = max(0.55, font_px * rng.uniform(1.05, 1.3) / 22.0)
    thickness = rng.choice((1, 2, 2))
    glyphs = [
        (_glyph(c, scale * rng.uniform(0.92, 1.08), thickness + (2 if c in ".,:" else 0)), c)
        for c in text
    ]
    advance = [int(g[1] * rng.uniform(0.98, 1.12)) + (8 if c == " " else 0) for g, c in glyphs]
    width = sum(advance)
    avail = x1 - x0
    if width > avail * 1.25 and width > 0:  # squeeze long values like people do
        f = avail * 1.2 / width
        advance = [int(a * f) for a in advance]
        width = sum(advance)
    x = x1 - width - 4 if align_right else x0 + rng.randint(0, 6)
    base_y = y0 + int((y1 - y0) * 0.1)
    slant = rng.uniform(-3.0, 5.0)
    out: tuple[int, int, int, int] | None = None
    for ((mask, _), _c), adv in zip(glyphs, advance, strict=True):
        angle = slant + rng.gauss(0, 3.0)
        h, w = mask.shape
        m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        rot = cv2.warpAffine(mask, m, (w, h), flags=cv2.INTER_LINEAR)
        jitter = int(rng.gauss(0, max(1.0, font_px * 0.06)))
        out = _union(out, _paste(img, rot, x, base_y - 6 + jitter, ink))
        x += adv
    if illegible:
        out = _make_illegible(img, text, out or box, rng, scale=scale, thickness=thickness, ink=ink)
    return out or box


def _make_illegible(
    img: np.ndarray,
    text: str,
    box: tuple[int, int, int, int],
    rng: random.Random,
    *,
    scale: float,
    thickness: int,
    ink: tuple[int, int, int],
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    out: tuple[int, int, int, int] | None = box
    # 1) write "corrected" digits over the original, slightly offset
    digits = "".join(rng.choice("0123456789") if c.isdigit() else c for c in text)
    x = x0 + rng.randint(-4, 6)
    for c in digits:
        mask, w = _glyph(c, scale * 1.15, thickness + 1)
        out = _union(out, _paste(img, mask, x, y0 - 8 + rng.randint(-3, 3), ink))
        x += int(w * 0.95)
    # 2) scribble across
    pts = []
    xx = x0 - 4
    while xx < x1 + 6:
        pts.append((xx, rng.randint(y0, y1)))
        xx += rng.randint(5, 12)
    if len(pts) >= 2:
        cv2.polylines(img, [np.array(pts, dtype=np.int32)], False, ink, 2, cv2.LINE_AA)
    # 3) smear (a thumb dragged through wet ink / carbon)
    bx0, by0, bx1, by1 = out or box
    bx0, by0 = max(0, bx0 - 6), max(0, by0 - 6)
    bx1, by1 = min(img.shape[1], bx1 + 6), min(img.shape[0], by1 + 6)
    region = img[by0:by1, bx0:bx1]
    if region.size:
        k = max(5, (bx1 - bx0) // 8) | 1
        kernel = np.zeros((k, k), np.float32)
        kernel[k // 2, :] = 1.0 / k
        img[by0:by1, bx0:bx1] = cv2.filter2D(region, -1, kernel)
    return (bx0, by0, bx1, by1)
