"""Raster helpers: input sniffing, HEIC conversion, EXIF strip + orientation, resize, CLAHE,
background sampling and pHash."""

from __future__ import annotations

import functools
import io
import warnings

import cv2
import imagehash
import numpy as np
from PIL import Image, ImageOps

from invoicedesk.contracts.preprocess import TARGET_LONG_EDGE, Box, RejectedInput

__all__ = [
    "MAX_PIXELS",
    "clahe_png",
    "encode_png",
    "heic_to_jpeg",
    "load_raster",
    "phash_hex",
    "resize_long_edge",
    "sample_background",
    "sniff_mime",
]

#: Decoded pixel cap for raster uploads (decompression-bomb guard): 60 MP, e.g. 9,000 x 6,600.
MAX_PIXELS = 60_000_000

_HEIF_BRANDS = {
    b"heic",
    b"heix",
    b"hevc",
    b"hevx",
    b"heim",
    b"heis",
    b"hevm",
    b"hevs",
    b"mif1",
    b"msf1",
}


def sniff_mime(data: bytes) -> str | None:
    """Content type from magic bytes (never from the filename or the client's header)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "image/heic"
    # PDF readers accept the header anywhere in the first 1 KB; so do we (but nothing later).
    if b"%PDF-" in data[:1024]:
        return "application/pdf"
    return None


@functools.cache
def _register_heif() -> None:
    import pillow_heif

    pillow_heif.register_heif_opener()


def heic_to_jpeg(data: bytes) -> bytes:
    """Decode HEIC/HEIF (pillow-heif), apply orientation, and re-encode as a metadata-free JPEG."""
    _register_heif()
    try:
        with Image.open(io.BytesIO(data)) as src:
            _check_pixels(src.size)
            rgb = _to_rgb(ImageOps.exif_transpose(src))
    except RejectedInput:
        raise
    except Exception as exc:
        raise RejectedInput("The HEIC photo could not be read.") from exc
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _check_pixels(size: tuple[int, int]) -> None:
    if size[0] * size[1] > MAX_PIXELS:
        raise RejectedInput("The image is too large (over 60 megapixels).")
    if min(size) < 1:
        raise RejectedInput("The image is empty.")


def _to_rgb(im: Image.Image) -> Image.Image:
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return im.convert("RGB")


def load_raster(data: bytes) -> tuple[Image.Image, bool, list[str]]:
    """Open a PNG/JPEG, apply EXIF orientation, drop all metadata.

    Returns ``(rgb_image, had_metadata, notes)``. The returned image is a fresh pixel buffer, so
    no EXIF, GPS, XMP or ICC data survives into anything we store.
    """
    notes: list[str] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
            _check_pixels(im.size)
            if getattr(im, "n_frames", 1) > 1:
                notes.append("animated image: only the first frame was used")
            im.load()
    except RejectedInput:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise RejectedInput("The image is too large (over 60 megapixels).") from exc
    except Exception as exc:
        raise RejectedInput("The image could not be read.") from exc
    exif = im.getexif()
    had_meta = (
        bool(len(exif))
        or any(k in im.info for k in ("exif", "xmp", "XML:com.adobe.xmp", "icc_profile", "comment"))
        or any(k.lower().startswith(("exif", "xml")) for k in im.info if isinstance(k, str))
    )
    orientation = exif.get(0x0112, 1)
    if orientation not in (None, 1):
        notes.append(f"EXIF orientation {orientation} applied")
    im = ImageOps.exif_transpose(im)
    rgb = _to_rgb(im)
    clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())
    return clean, had_meta, notes


def resize_long_edge(img: Image.Image, target: int = TARGET_LONG_EDGE) -> tuple[Image.Image, float]:
    """Resize so the long edge is exactly ``target`` px (up or down). Returns ``(img, scale)``."""
    w, h = img.size
    scale = target / max(w, h)
    size = (target, max(1, round(h * scale))) if w >= h else (max(1, round(w * scale)), target)
    if size == (w, h):
        return img, 1.0
    resample = Image.Resampling.LANCZOS if scale < 1 else Image.Resampling.BICUBIC
    return img.resize(size, resample), scale


def encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


def clahe_png(rgb: np.ndarray, *, clip_limit: float = 40.0, tile: int = 8) -> bytes:
    """Contrast-limited adaptive histogram equalization on luminance (faint-text pass input).

    A high clip limit is deliberate: 5%-contrast grey text on white needs strong local
    stretching. The output is only ever OCR'd, never shown or sent to a model.
    """
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    eq = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(tile, tile)).apply(gray)
    ok, buf = cv2.imencode(".png", eq)
    if not ok:  # pragma: no cover - cv2 encode of a valid array does not fail
        raise RuntimeError("PNG encode failed")
    return buf.tobytes()


def sample_background(rgb: np.ndarray, box: Box, *, ring: int = 3) -> tuple[int, int, int] | None:
    """Median colour of a thin ring just outside ``box`` (falls back to the box interior).

    The ring avoids the glyphs themselves, so white-on-white text samples white and light text on
    a dark band samples the band. ``None`` when the box lies entirely outside the image.
    """
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = box
    ix0, iy0, ix1, iy1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if ix0 >= ix1 or iy0 >= iy1:
        return None
    ox0, oy0 = max(0, x0 - ring), max(0, y0 - ring)
    ox1, oy1 = min(w, x1 + ring), min(h, y1 + ring)
    outer = rgb[oy0:oy1, ox0:ox1].reshape(-1, 3)
    mask = np.ones((oy1 - oy0, ox1 - ox0), dtype=bool)
    mask[iy0 - oy0 : iy1 - oy0, ix0 - ox0 : ix1 - ox0] = False
    pixels = outer[mask.reshape(-1)]
    if len(pixels) == 0:
        pixels = rgb[iy0:iy1, ix0:ix1].reshape(-1, 3)
    med = np.median(pixels, axis=0)
    return (int(med[0]), int(med[1]), int(med[2]))


def phash_hex(img: Image.Image) -> str:
    return str(imagehash.phash(img))
