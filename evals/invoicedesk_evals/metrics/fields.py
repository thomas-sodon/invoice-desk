"""Per-field precision / recall / F1 over the 13 header fields (DESIGN §5.6).

Counting (key-information-extraction convention, null-vs-value handled explicitly):

====================  ==========================  =================
gold                  prediction                  counts
====================  ==========================  =================
value                 same normalized value       TP
value                 different value             FP + FN
value                 null                        FN
null                  value                       FP
null                  null                        nothing (TN)
====================  ==========================  =================

Header-field micro F1 sums TP/FP/FN over all fields and documents.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from invoicedesk.contracts.extraction import HEADER_FIELDS, InvoiceExtraction, field_value

from invoicedesk_evals.normalize import Normalized, norm_text, normalize_field

__all__ = [
    "Counts",
    "FieldOutcome",
    "f1_by_field",
    "micro",
    "score_header_fields",
    "score_value",
]


@dataclass(frozen=True, slots=True)
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(self.tp + other.tp, self.fp + other.fp, self.fn + other.fn)

    @property
    def precision(self) -> float | None:
        d = self.tp + self.fp
        return self.tp / d if d else None

    @property
    def recall(self) -> float | None:
        d = self.tp + self.fn
        return self.tp / d if d else None

    @property
    def f1(self) -> float | None:
        d = 2 * self.tp + self.fp + self.fn
        return 2 * self.tp / d if d else None


@dataclass(frozen=True, slots=True)
class FieldOutcome:
    name: str
    gold: Normalized
    pred: Normalized
    counts: Counts

    @property
    def correct(self) -> bool | None:
        """``None`` when both sides are null (nothing to be right about)."""
        if self.gold is None and self.pred is None:
            return None
        return self.gold == self.pred


def score_value(gold: Normalized, pred: Normalized) -> Counts:
    if gold is None and pred is None:
        return Counts()
    if gold is None:
        return Counts(fp=1)
    if pred is None:
        return Counts(fn=1)
    if gold == pred:
        return Counts(tp=1)
    return Counts(fp=1, fn=1)


def _as_extraction(ex: InvoiceExtraction | Mapping[str, Any]) -> InvoiceExtraction:
    return ex if isinstance(ex, InvoiceExtraction) else InvoiceExtraction.model_validate(ex)


def score_header_fields(
    gold: InvoiceExtraction | Mapping[str, Any],
    pred: InvoiceExtraction | Mapping[str, Any] | None,
    *,
    gold_vendor_id: str | None = None,
    pred_vendor_id: str | None = None,
    vendor_resolved: bool = False,
    exclude: Iterable[str] = (),
    fields: Iterable[str] = HEADER_FIELDS,
) -> dict[str, FieldOutcome]:
    """Score the header fields of one document.

    ``vendor_resolved=True`` means the run produced a resolved vendor (R1's ``vendor_id`` field
    row exists, possibly null). Then, when the gold record has a vendor_id, ``vendor_name`` is
    right iff the resolved id equals the gold id. ``exclude`` drops fields from scoring (e.g.
    ones the gold labels as illegible, which no reader can get right).
    """
    g = _as_extraction(gold)
    p = None if pred is None else _as_extraction(pred)
    skip = set(exclude)
    out: dict[str, FieldOutcome] = {}
    for name in fields:
        if name in skip:
            continue
        gv: Normalized = normalize_field(name, field_value(g, name))
        pv: Normalized = None if p is None else normalize_field(name, field_value(p, name))
        if name == "vendor_name" and vendor_resolved and gold_vendor_id is not None:
            # Resolved-vendor comparison: the printed name only matters through what it
            # resolved to. A null name still counts as missing.
            gv = f"vendor:{gold_vendor_id}"
            pv = None if pv is None else f"vendor:{pred_vendor_id}"
        elif name == "vendor_name":
            gv, pv = norm_text(gv), norm_text(pv)
        out[name] = FieldOutcome(name, gv, pv, score_value(gv, pv))
    return out


def micro(outcomes: Iterable[Mapping[str, FieldOutcome]]) -> Counts:
    total = Counts()
    for doc in outcomes:
        for o in doc.values():
            total = total + o.counts
    return total


def f1_by_field(outcomes: Iterable[Mapping[str, FieldOutcome]]) -> dict[str, Counts]:
    by: dict[str, Counts] = {}
    for doc in outcomes:
        for name, o in doc.items():
            by[name] = by.get(name, Counts()) + o.counts
    return {n: by[n] for n in HEADER_FIELDS if n in by}
