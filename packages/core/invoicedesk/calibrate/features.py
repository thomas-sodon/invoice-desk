"""Per-field calibration signals (M6): raw self-report, OCR agreement, arithmetic consistency,
cross-tier agreement and handwriting. Pure functions of the extraction and the OCR words."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping

from rapidfuzz import fuzz

from invoicedesk.contracts.calibration import FieldSignals
from invoicedesk.contracts.extraction import (
    DateField,
    InvoiceExtraction,
    MoneyField,
    TextField,
    iter_header_fields,
)
from invoicedesk.contracts.preprocess import OcrWord

__all__ = [
    "FEATURE_NAMES",
    "build_field_signals",
    "feature_vector",
    "iter_scored_fields",
    "ocr_agreement",
]

FieldModel = TextField | MoneyField | DateField

#: Order of :func:`feature_vector` (persisted with fitted artifacts).
FEATURE_NAMES: tuple[str, ...] = (
    "raw_conf",
    "ocr_agreement",
    "arith_ok",
    "arith_known",
    "cross_agree",
    "cross_known",
    "has_source_words",
    "handwriting",
)

_ALNUM = re.compile(r"[^0-9a-z]+")
_DIGITS = re.compile(r"\D+")
_DATE_FORMATS = (
    "%Y%m%d",
    "%m%d%Y",
    "%d%m%Y",
    "%m%d%y",
    "%d%m%y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
)


def _alnum(s: str) -> str:
    return _ALNUM.sub("", s.casefold())


def _money_candidates(cents: int) -> list[str]:
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    out = [f"{sign}{c // 100}{c % 100:02d}"]
    if c % 100 == 0:
        out.append(f"{sign}{c // 100}")
    return [o.lstrip("-") for o in out]


def _date_candidates(iso: str) -> list[str]:
    try:
        d = dt.date.fromisoformat(iso)
    except ValueError:
        return [_alnum(iso)]
    cands = {_alnum(d.strftime(f)) for f in _DATE_FORMATS}
    # unpadded month/day variants ("9/8/2026")
    cands.add(f"{d.month}{d.day}{d.year}")
    cands.add(f"{d.day}{d.month}{d.year}")
    return sorted(cands)


def ocr_agreement(f: FieldModel, cited: list[str]) -> float:
    """0..1 agreement between the extracted value and the text of the words it cites.

    Money compares digit strings (``124000`` vs ``"$1,240.00"``), dates compare against common
    printed formats, text uses the best of rapidfuzz ``ratio``/``token_sort_ratio``/
    ``partial_ratio`` (a cited label like "Invoice #:" should not sink a correct value).
    A null value, or a value citing no words, gets 0.
    """
    if not cited:
        return 0.0
    joined = " ".join(cited)
    if isinstance(f, MoneyField):
        if f.value_cents is None:
            return 0.0
        digits = _DIGITS.sub("", joined)
        if not digits:
            return 0.0
        best = 0.0
        for cand in _money_candidates(f.value_cents):
            if cand == digits:
                return 1.0
            best = max(best, fuzz.ratio(cand, digits) / 100.0)
        return best
    if isinstance(f, DateField):
        if not f.value_iso:
            return 0.0
        text = _alnum(joined)
        return max(fuzz.ratio(c, text) / 100.0 for c in _date_candidates(f.value_iso))
    if f.value is None:
        return 0.0
    a, b = _alnum(f.value), _alnum(joined)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = fuzz.ratio(a, b)
    tsr = fuzz.token_sort_ratio(f.value.casefold(), joined.casefold())
    # partial match only counts when the value is the bulk of the cited text
    partial = fuzz.partial_ratio(a, b) * min(1.0, len(a) / max(len(b), 1) + 0.25)
    return min(1.0, max(ratio, tsr, partial) / 100.0)


def iter_scored_fields(ex: InvoiceExtraction) -> list[tuple[str, FieldModel]]:
    """Header fields (12) plus every line-item cell, named ``line_items[i].<cell>``."""
    out: list[tuple[str, FieldModel]] = list(iter_header_fields(ex))
    for i, li in enumerate(ex.line_items):
        for cell in ("description", "quantity", "unit_price", "amount"):
            out.append((f"line_items[{i}].{cell}", getattr(li, cell)))
    return out


def build_field_signals(
    ex: InvoiceExtraction,
    words_by_id: Mapping[int, OcrWord],
    *,
    arithmetic: Mapping[str, bool | None] | None = None,
    agreement: Mapping[str, bool] | None = None,
) -> list[FieldSignals]:
    """One :class:`FieldSignals` per scored field. Line-item cells inherit the
    ``line_items`` cross-tier agreement."""
    arithmetic = arithmetic or {}
    agreement = agreement or {}
    out: list[FieldSignals] = []
    for name, f in iter_scored_fields(ex):
        cited_words = [words_by_id[i] for i in f.source.word_ids if i in words_by_id]
        cited = [w.text for w in cited_words]
        agree_key = "line_items" if name.startswith("line_items[") else name
        out.append(
            FieldSignals(
                field=name,
                raw_conf=f.confidence,
                ocr_agreement=ocr_agreement(f, cited),
                arithmetic_ok=arithmetic.get(name),
                cross_tier_agree=agreement.get(agree_key),
                has_source_words=bool(cited_words),
                handwriting=ex.handwriting_present,
            )
        )
    return out


def _tri(v: bool | None) -> tuple[float, float]:
    if v is None:
        return 0.5, 0.0
    return (1.0 if v else 0.0), 1.0


def feature_vector(s: FieldSignals) -> list[float]:
    a, ak = _tri(s.arithmetic_ok)
    c, ck = _tri(s.cross_tier_agree)
    return [
        s.raw_conf,
        s.ocr_agreement,
        a,
        ak,
        c,
        ck,
        1.0 if s.has_source_words else 0.0,
        1.0 if s.handwriting else 0.0,
    ]
