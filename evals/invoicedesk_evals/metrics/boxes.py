"""Box hit rate (DESIGN §5.6): a field's box is a hit when IoU >= 0.5 with the gold box on the
same page. Evaluated on native and scanned documents only (gold boxes on photos and
handwriting are not geometrically exact)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from invoicedesk.contracts.extraction import HEADER_FIELDS

__all__ = ["BOX_MEDIA", "IOU_THRESHOLD", "BoxScore", "iou", "score_boxes"]

IOU_THRESHOLD = 0.5
BOX_MEDIA = frozenset({"native", "scanned"})

Box = Sequence[float]


def iou(a: Box, b: Box) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


@dataclass(frozen=True, slots=True)
class BoxScore:
    hits: int
    total: int


def score_boxes(
    gold_boxes: Mapping[str, Mapping[str, Any]],
    pred_boxes: Mapping[str, Mapping[str, Any]],
    *,
    medium: str,
    box_eval: bool = True,
    skip: Sequence[str] = (),
) -> BoxScore:
    """``gold_boxes``/``pred_boxes``: ``{field: {"box": [x0,y0,x1,y1], "page": n}}``. Only
    header fields with a gold box count; a missing predicted box is a miss."""
    if not box_eval or medium not in BOX_MEDIA:
        return BoxScore(0, 0)
    hits = total = 0
    for name in HEADER_FIELDS:
        g = gold_boxes.get(name)
        if g is None or name in skip:
            continue
        total += 1
        p = pred_boxes.get(name)
        if p is None or p.get("box") is None:
            continue
        if int(p.get("page", 0)) == int(g.get("page", 0)) and iou(p["box"], g["box"]) >= (
            IOU_THRESHOLD
        ):
            hits += 1
    return BoxScore(hits, total)
