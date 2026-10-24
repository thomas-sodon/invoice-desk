"""Augmentation: known transforms keep boxes valid; faint text has the requested contrast."""

from __future__ import annotations

import random

import numpy as np
from invoicedesk_data.generator import augment


def _page_with_block() -> tuple[np.ndarray, tuple[int, int, int, int]]:
    img = np.full((1600, 1237, 3), 255, np.uint8)
    box = (400, 600, 700, 660)
    img[box[1] : box[3], box[0] : box[2]] = 20
    return img, box


def _dark_bbox(img: np.ndarray) -> tuple[int, int, int, int]:
    gray = img.mean(axis=2)
    ys, xs = np.nonzero(gray < 90)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def test_scan_transform_maps_boxes() -> None:
    for seed in range(4):
        img, box = _page_with_block()
        out, m, params = augment.scan(img, random.Random(seed))
        assert out.shape == img.shape
        mapped = augment.transform_box(box, m, (out.shape[1], out.shape[0]))
        assert _iou(mapped, _dark_bbox(out)) > 0.85, (seed, params)


def test_photo_homography_maps_boxes() -> None:
    for seed in range(4):
        img, box = _page_with_block()
        out, h, _ = augment.photo(img, random.Random(seed), crumple=bool(seed % 2))
        assert out.shape == (1600, 1200, 3)
        mapped = augment.transform_box(box, h, (1200, 1600))
        # the desk background can be dark; look only inside the page area around the box
        x0, y0, x1, y1 = mapped
        crop = out[max(0, y0 - 30) : y1 + 30, max(0, x0 - 30) : x1 + 30]
        cx0, cy0, cx1, cy1 = _dark_bbox(crop)
        found = (
            cx0 + max(0, x0 - 30),
            cy0 + max(0, y0 - 30),
            cx1 + max(0, x0 - 30),
            cy1 + max(0, y0 - 30),
        )
        assert _iou(mapped, found) > 0.75, seed


def test_augment_is_deterministic() -> None:
    img, _ = _page_with_block()
    a, _, _ = augment.scan(img, random.Random(5))
    b, _, _ = augment.scan(img, random.Random(5))
    assert np.array_equal(a, b)
    c, _, _ = augment.photo(img, random.Random(5))
    d, _, _ = augment.photo(img, random.Random(5))
    assert np.array_equal(c, d)


def test_faint_text_contrast() -> None:
    img = np.full((400, 1200, 3), 250, np.uint8)
    boxes = augment.draw_faint_text(
        img, ["faint instruction"], origin=(50, 50), contrast=0.05, size_px=40
    )
    x0, y0, x1, y1 = boxes[0]
    region = img[y0:y1, x0:x1].astype(int)
    darkest = region.min()
    assert darkest >= int(250 * 0.95) - 1  # never darker than 5 % below the paper
    assert darkest < 250  # but it is there


def test_image_pdf_is_a_single_letter_page() -> None:
    import pymupdf

    img, _ = _page_with_block()
    data = augment.image_pdf(img)
    doc = pymupdf.open(stream=data, filetype="pdf")
    assert doc.page_count == 1
    assert (doc[0].rect.width, doc[0].rect.height) == (612, 792)
    assert str(doc[0].get_text()).strip() == ""
    doc.close()
