"""Deterministic reconcile (two tiers) and adjudication (three tiers) of independent extractions.

Each tier is an independent single-shot reading of the same document. Code (not a model) merges
them, field by field:

* Values are compared after normalization (cents, ISO dates, case and whitespace folded text;
  IDs fold case and drop spaces only, so "INV-2231" and "INV 2231" still count as different).
* A value backed by a strict majority wins. With no majority, the strongest tier's value wins
  (Opus > Sonnet > Haiku), and the field stays marked as a disagreement, so its cross-tier
  agreement signal is ``False`` and the calibrator lowers its confidence.
* Among the tiers that agree on the winner, the field object comes from the strongest one that
  cites OCR words (so boxes stay anchored to word IDs whenever any tier found them).
* Safety-relevant evidence is never dropped: ``embedded_instructions`` and
  ``legibility_issues`` are the union over tiers, ``handwriting_present`` is an OR, and a
  remit-to/bank value seen by any tier is kept (null never outvotes a value there).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from invoicedesk.contracts.extraction import (
    DateField,
    InvoiceExtraction,
    MoneyField,
    TextField,
    iter_header_fields,
)

__all__ = ["ReconcileResult", "TierOutput", "normalize_value", "reconcile"]

TierName = Literal["haiku", "sonnet", "opus"]
_STRENGTH: dict[str, int] = {"haiku": 0, "sonnet": 1, "opus": 2}
_ID_FIELDS = frozenset({"invoice_number", "work_order_number", "vendor_tax_id_last4"})
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class TierOutput:
    tier: TierName
    extraction: InvoiceExtraction


@dataclass
class ReconcileResult:
    merged: InvoiceExtraction
    #: field -> True if a majority of tiers (all, with two tiers) agree; absent = single tier.
    agreement: dict[str, bool] = field(default_factory=dict)
    #: fields on which the tiers did not all agree (reported; includes resolved ones).
    disagreements: list[str] = field(default_factory=list)
    #: fields with no majority at all (the strongest tier's value was used).
    unresolved: list[str] = field(default_factory=list)


def normalize_value(name: str, f: TextField | MoneyField | DateField) -> str | int | None:
    if isinstance(f, MoneyField):
        return f.value_cents
    if isinstance(f, DateField):
        return f.value_iso
    if f.value is None:
        return None
    v = f.value.casefold().strip()
    if name in _ID_FIELDS:
        return _WS.sub("", v)
    return _WS.sub(" ", v).strip(" .,;:")


def _has_words(f: TextField | MoneyField | DateField) -> bool:
    return bool(f.source.word_ids)


def _vote(
    keys: Sequence[Any], strengths: Sequence[int], *, ignore_none: bool = False
) -> tuple[Any, bool, bool]:
    """Return ``(winning_key, majority, all_agree)``."""
    candidates = [(k, s) for k, s in zip(keys, strengths, strict=True)]
    if ignore_none and any(k is not None for k, _ in candidates):
        candidates = [(k, s) for k, s in candidates if k is not None]
    counts: dict[Any, int] = {}
    best_strength: dict[Any, int] = {}
    for k, s in candidates:
        counts[k] = counts.get(k, 0) + 1
        best_strength[k] = max(best_strength.get(k, -1), s)
    n = len(keys)
    winner = max(counts, key=lambda k: (counts[k], best_strength[k]))
    majority = counts[winner] * 2 > n
    all_agree = len(set(keys)) == 1
    return winner, majority, all_agree


def reconcile(outputs: Sequence[TierOutput]) -> ReconcileResult:
    """Merge 1-3 independent tier outputs (any order)."""
    if not outputs:
        raise ValueError("nothing to reconcile")
    outs = sorted(outputs, key=lambda o: _STRENGTH[o.tier])
    strongest = outs[-1].extraction
    if len(outs) == 1:
        return ReconcileResult(merged=strongest)

    strengths = [_STRENGTH[o.tier] for o in outs]
    exs = [o.extraction for o in outs]
    data: dict[str, Any] = {}
    agreement: dict[str, bool] = {}
    disagreements: list[str] = []
    unresolved: list[str] = []

    for name, _ in iter_header_fields(strongest):
        fields_ = [getattr(e, name) for e in exs]
        keys = [normalize_value(name, f) for f in fields_]
        winner, majority, all_agree = _vote(
            keys, strengths, ignore_none=name == "remit_to_bank_details"
        )
        agreeing = [(s, f) for k, s, f in zip(keys, strengths, fields_, strict=True) if k == winner]
        agreeing.sort(key=lambda sf: (_has_words(sf[1]), sf[0]))
        data[name] = agreeing[-1][1]
        agreement[name] = all_agree if len(outs) == 2 else majority
        if not all_agree:
            disagreements.append(name)
        if not majority:
            unresolved.append(name)

    # doc_type
    winner, majority, all_agree = _vote([e.doc_type for e in exs], strengths)
    data["doc_type"] = winner
    agreement["doc_type"] = all_agree if len(outs) == 2 else majority
    if not all_agree:
        disagreements.append("doc_type")
    if not majority:
        unresolved.append("doc_type")

    # line items: vote on the amount column (descriptions are worded differently by tiers)
    li_keys = [tuple(li.amount.value_cents for li in e.line_items) for e in exs]
    winner, majority, all_agree = _vote(li_keys, strengths)
    chosen = [e for e, k in zip(exs, li_keys, strict=True) if k == winner][-1]
    data["line_items"] = chosen.line_items
    agreement["line_items"] = all_agree if len(outs) == 2 else majority
    if not all_agree:
        disagreements.append("line_items")
    if not majority:
        unresolved.append("line_items")

    data["handwriting_present"] = any(e.handwriting_present for e in exs)
    data["legibility_issues"] = _union(e.legibility_issues for e in exs)
    data["embedded_instructions"] = _union(e.embedded_instructions for e in exs)

    merged = InvoiceExtraction.model_validate(
        {k: (v.model_dump() if hasattr(v, "model_dump") else v) for k, v in data.items()}
        | {"line_items": [li.model_dump() for li in data["line_items"]]}
    )
    return ReconcileResult(
        merged=merged, agreement=agreement, disagreements=disagreements, unresolved=unresolved
    )


def _union(lists: Any) -> list[str]:
    seen: dict[str, None] = {}
    for lst in lists:
        for item in lst:
            seen.setdefault(item, None)
    return list(seen)
