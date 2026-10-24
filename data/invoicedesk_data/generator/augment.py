"""Scan and phone-photo simulation with box-preserving geometry (DESIGN §6.2 steps 3-4).

Default degradations are OpenCV/NumPy only: paper tint and fibre noise, ink fade, Gaussian
blur, sensor noise, JPEG round trip and a small **known affine** skew. When the optional
``augraphy`` extra is installed (``uv sync --extra augraphy``) and ``use_augraphy=True``, its
non-geometric ink and paper phases run first; geometry is always ours, so gold boxes stay valid
through the recorded transform.

The "phone photo" path stands in for photographing printed invoices (which cannot be done in
this environment; see docs/GAPS.md): perspective warp onto a desk background (homography
recorded), lighting gradient, crumple shading and JPEG. Box hit rate is not evaluated on
photos (§6.2), but transformed boxes are still recorded.

Every function takes a ``random.Random`` and is deterministic for a given seed.
"""

from __future__ import annotations

import contextlib
import io
import random
from collections.abc import Iterable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

__all__ = [
    "Box",
    "draw_faint_text",
    "encode_jpeg",
    "encode_png",
    "image_pdf",
    "photo",
    "scan",
    "transform_box",
]

Box = tuple[int, int, int, int]


def transform_box(box: Box, m: np.ndarray, size: tuple[int, int]) -> Box:
    """Map a box through a 3x3 transform; return the clipped axis-aligned bounding box."""
    x0, y0, x1, y1 = box
    pts = np.array([[x0, y0, 1], [x1, y0, 1], [x1, y1, 1], [x0, y1, 1]], dtype=np.float64).T
    q = m @ pts
    q = q[:2] / q[2]
    w, h = size
    nx0 = int(np.clip(np.floor(q[0].min()), 0, w))
    ny0 = int(np.clip(np.floor(q[1].min()), 0, h))
    nx1 = int(np.clip(np.ceil(q[0].max()), 0, w))
    ny1 = int(np.clip(np.ceil(q[1].max()), 0, h))
    return (nx0, ny0, nx1, ny1)


def _np_rng(rng: random.Random) -> np.random.Generator:
    return np.random.default_rng(rng.getrandbits(63))


def _augraphy(img: np.ndarray, rng: random.Random) -> np.ndarray:  # pragma: no cover - extra
    import augraphy  # pyright: ignore[reportMissingImports]

    seed = rng.getrandbits(31)
    random.seed(seed)
    np.random.seed(seed)
    pipeline = augraphy.AugraphyPipeline(
        ink_phase=[augraphy.InkBleed(p=0.6), augraphy.LowInkRandomLines(p=0.4)],
        paper_phase=[augraphy.ColorPaper(p=0.5), augraphy.NoiseTexturize(p=0.6)],
        post_phase=[augraphy.DirtyDrum(p=0.3), augraphy.SubtleNoise(p=0.6)],
    )
    out = pipeline(img)
    if out.ndim == 2:
        out = cv2.cvtColor(out, cv2.COLOR_GRAY2RGB)
    return out


def _paper_and_ink(img: np.ndarray, rng: random.Random, nprng: np.random.Generator) -> np.ndarray:
    f = img.astype(np.float32)
    # ink fade: pull dark pixels slightly up
    fade = rng.uniform(0.0, 0.18)
    f = f + (255.0 - f) * fade * (f < 128)
    # paper tint
    tint = np.array([rng.uniform(-6, 2), rng.uniform(-6, 2), rng.uniform(-12, 0)], np.float32)
    f = f + tint
    # fibre noise (low-frequency)
    small = nprng.normal(0, rng.uniform(2.0, 5.0), (img.shape[0] // 16 + 1, img.shape[1] // 16 + 1))
    fibre = cv2.resize(small.astype(np.float32), (img.shape[1], img.shape[0]))
    f = f + fibre[..., None]
    return np.clip(f, 0, 255).astype(np.uint8)


def _sensor(
    img: np.ndarray, rng: random.Random, nprng: np.random.Generator, sigma: float
) -> np.ndarray:
    noisy = img.astype(np.float32) + nprng.normal(0, sigma, img.shape).astype(np.float32)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def _jpeg_roundtrip(img: np.ndarray, quality: int) -> np.ndarray:
    ok, buf = cv2.imencode(
        ".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality]
    )
    if not ok:  # pragma: no cover
        raise RuntimeError("jpeg encode failed")
    decoded = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if decoded is None:  # pragma: no cover
        raise RuntimeError("jpeg decode failed")
    return cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)


def scan(
    img: np.ndarray, rng: random.Random, *, use_augraphy: bool = False
) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    """Flatbed/MFP scan. Output has the input's size. Returns ``(image, M3x3, params)``."""
    nprng = _np_rng(rng)
    h, w = img.shape[:2]
    out = img.copy()
    if use_augraphy:
        with contextlib.suppress(ImportError):
            out = _augraphy(out, rng)
    out = _paper_and_ink(out, rng, nprng)
    angle = rng.uniform(-1.6, 1.6)
    tx, ty = rng.uniform(-10, 10), rng.uniform(-10, 10)
    m2 = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    m2[:, 2] += (tx, ty)
    out = cv2.warpAffine(out, m2, (w, h), flags=cv2.INTER_LINEAR, borderValue=(250, 250, 248))
    sigma_blur = rng.uniform(0.3, 0.9)
    out = cv2.GaussianBlur(out, (0, 0), sigma_blur)
    noise = rng.uniform(2.0, 6.0)
    out = _sensor(out, rng, nprng, noise)
    quality = rng.randint(55, 85)
    out = _jpeg_roundtrip(out, quality)
    m3 = np.vstack([m2, [0.0, 0.0, 1.0]])
    params = {
        "angle": angle,
        "tx": tx,
        "ty": ty,
        "blur": sigma_blur,
        "noise": noise,
        "jpeg_quality": float(quality),
    }
    return out, m3, params


def _desk(h: int, w: int, rng: random.Random, nprng: np.random.Generator) -> np.ndarray:
    base = np.array(
        rng.choice([(122, 92, 64), (150, 118, 84), (88, 88, 92), (176, 170, 160), (60, 62, 70)]),
        np.float32,
    )
    grain = cv2.resize(
        nprng.normal(0, 14, (h // 24 + 1, 6)).astype(np.float32),
        (w, h),
        interpolation=cv2.INTER_CUBIC,
    )
    desk = base[None, None, :] + grain[..., None]
    return np.clip(desk, 0, 255).astype(np.uint8)


def photo(
    img: np.ndarray,
    rng: random.Random,
    *,
    out_size: tuple[int, int] = (1200, 1600),
    crumple: bool | None = None,
) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    """Phone photo of a printed page on a desk. ``out_size`` is ``(w, h)``. Returns
    ``(image, H3x3, params)`` where ``H`` maps page pixels to photo pixels."""
    nprng = _np_rng(rng)
    h, w = img.shape[:2]
    ow, oh = out_size
    page = _paper_and_ink(img, rng, nprng)
    # destination quad: page fills ~80-92 % of the frame with perspective
    mx, my = ow * rng.uniform(0.04, 0.09), oh * rng.uniform(0.03, 0.07)
    j = lambda s: rng.uniform(-s, s)  # noqa: E731
    dst = np.array(
        [
            [mx + j(30), my + j(25)],
            [ow - mx + j(30), my + j(25)],
            [ow - mx + j(40), oh - my + j(30)],
            [mx + j(40), oh - my + j(30)],
        ],
        dtype=np.float32,
    )
    src = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    hmat = cv2.getPerspectiveTransform(src, dst)
    desk = _desk(oh, ow, rng, nprng)
    warped = cv2.warpPerspective(page, hmat, (ow, oh), flags=cv2.INTER_LINEAR)
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), hmat, (ow, oh))
    m = (mask.astype(np.float32) / 255.0)[..., None]
    comp = warped.astype(np.float32) * m + desk.astype(np.float32) * (1 - m)
    # lighting: linear gradient + soft vignette + optional crumple shading
    yy, xx = np.mgrid[0:oh, 0:ow].astype(np.float32)
    ang = rng.uniform(0, 2 * np.pi)
    grad = (np.cos(ang) * (xx / ow - 0.5) + np.sin(ang) * (yy / oh - 0.5)) * rng.uniform(0.15, 0.35)
    vign = ((xx / ow - 0.5) ** 2 + (yy / oh - 0.5) ** 2) * rng.uniform(0.2, 0.5)
    light = 1.0 - grad - vign
    do_crumple = rng.random() < 0.5 if crumple is None else crumple
    if do_crumple:
        field = nprng.normal(0, 1, (oh // 80 + 2, ow // 80 + 2)).astype(np.float32)
        field = cv2.resize(field, (ow, oh), interpolation=cv2.INTER_CUBIC)
        field = cv2.GaussianBlur(field, (0, 0), 9)
        light = light * (1.0 + 0.035 * np.clip(field / (field.std() + 1e-6), -2.5, 2.5))
    warm = np.array([1.0, rng.uniform(0.95, 1.0), rng.uniform(0.86, 0.97)], np.float32)
    comp = comp * light[..., None] * warm[None, None, :]
    out = np.clip(comp, 0, 255).astype(np.uint8)
    blur = rng.uniform(0.5, 1.3)
    out = cv2.GaussianBlur(out, (0, 0), blur)
    noise = rng.uniform(3.0, 7.0)
    out = _sensor(out, rng, nprng, noise)
    quality = rng.randint(72, 90)
    out = _jpeg_roundtrip(out, quality)
    params = {
        "blur": blur,
        "noise": noise,
        "jpeg_quality": float(quality),
        "crumple": float(do_crumple),
    }
    return out, hmat.astype(np.float64), params


def draw_faint_text(
    img: np.ndarray,
    lines: Iterable[str],
    *,
    origin: tuple[int, int],
    contrast: float,
    size_px: int = 30,
) -> list[Box]:
    """Print grey text whose contrast against the local paper is ``contrast`` (0.05 = 5 %).

    Returns one box per line (page pixels). Modifies ``img`` in place."""
    pil = Image.fromarray(img)
    draw = ImageDraw.Draw(pil)
    font = ImageFont.load_default(size=size_px)
    x, y = origin
    boxes: list[Box] = []
    for line in lines:
        x0, y0, x1, y1 = (int(v) for v in draw.textbbox((x, y), line, font=font))
        paper = np.median(
            img[max(0, y0 - 4) : y1 + 4, max(0, x0 - 4) : x1 + 4].reshape(-1, 3), axis=0
        )
        grey = tuple(round(float(c) * (1.0 - contrast)) for c in paper)
        draw.text((x, y), line, font=font, fill=grey)
        boxes.append((x0, y0, x1, y1))
        y = y1 + size_px // 2
    img[:] = np.asarray(pil)
    return boxes


def encode_jpeg(img: np.ndarray, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue()


def encode_png(img: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def image_pdf(img: np.ndarray, *, quality: int = 85) -> bytes:
    """Wrap a page raster in a US-letter image-only PDF (a typical MFP "scan to PDF")."""
    import pymupdf

    from invoicedesk_data.generator.render import normalize_pdf

    doc = pymupdf.open()
    try:
        page = doc.new_page(width=612, height=792)
        page.insert_image(page.rect, stream=encode_jpeg(img, quality), keep_proportion=False)
        return normalize_pdf(doc.tobytes(garbage=3, deflate=True, no_new_id=True))
    finally:
        doc.close()
