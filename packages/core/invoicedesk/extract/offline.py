"""Deterministic offline stand-in for Claude, used by ``invoicedesk extract`` when no recording
exists and no API key is configured. It is NOT a model: it reads the OCR word list in the user
turn with simple label/regex heuristics and answers in the same structured shapes, so the whole
tiered path (calibration, reconcile, boxes) runs end to end without network access.

Accuracy is deliberately modest; evals never use it (they use recordings or live calls).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from invoicedesk.config.models import ModelRole
from invoicedesk.contracts.llm import LLMRequest, TextBlock
from invoicedesk.fakes.llm import FakeLLM

__all__ = ["OfflineWord", "heuristic_extraction", "heuristic_responder", "offline_llm"]

_WORD_RE = re.compile(r'^\[(\d+)\] (".*") \((-?\d+),(-?\d+),(-?\d+),(-?\d+)\)$')
_PAGE_RE = re.compile(r"^--- page (\d+)")
_MONEY_RE = re.compile(r"^\(?-?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})$|^\(?-?\$?\d+\.\d{2}$")
_WO_RE = re.compile(r"^W\.?O\.?-?\d{2,}$", re.I)
_INV_RE = re.compile(r"^(?!W\.?O)[A-Z]{1,5}-?\d{3,}[A-Z]?$", re.I)
_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")
_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), ("y", "m", "d")),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), ("m", "d", "y")),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$"), ("m", "d", "y2")),
)
_INSTRUCTION_RE = re.compile(
    r"\b(ai|assistant|system:|ignore (all )?previous|pre-?approved|skip (review|checks)|"
    r"automation|update (our|the) bank|new (bank|account)|change (of )?(bank|remit))\b",
    re.I,
)
_BANK_RE = re.compile(r"\b(remit|bank|routing|account|acct|aba|iban|swift)\b", re.I)


@dataclass(frozen=True)
class OfflineWord:
    id: int
    page: int
    text: str
    box: tuple[int, int, int, int]

    @property
    def yc(self) -> float:
        return (self.box[1] + self.box[3]) / 2


def _parse_words(text: str) -> list[OfflineWord]:
    words: list[OfflineWord] = []
    page = 0
    for line in text.splitlines():
        pm = _PAGE_RE.match(line)
        if pm:
            page = int(pm.group(1)) - 1
            continue
        m = _WORD_RE.match(line)
        if m:
            words.append(
                OfflineWord(
                    int(m.group(1)),
                    page,
                    json.loads(m.group(2)),
                    (int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6))),
                )
            )
    return words


def _rows(words: list[OfflineWord]) -> list[list[OfflineWord]]:
    rows: list[list[OfflineWord]] = []
    for w in sorted(words, key=lambda w: (w.page, w.yc, w.box[0])):
        if rows and rows[-1][0].page == w.page and abs(rows[-1][0].yc - w.yc) <= 10:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w.box[0]) for r in rows]


def _money_cents(text: str) -> int | None:
    t = text.strip().replace("$", "").replace(",", "")
    neg = t.startswith("(") or t.startswith("-")
    t = t.strip("()-")
    try:
        whole, _, frac = t.partition(".")
        cents = int(whole) * 100 + int((frac + "00")[:2])
    except ValueError:
        return None
    return -cents if neg else cents


def _date_iso(text: str) -> str | None:
    t = text.strip().rstrip(".,")
    for rx, order in _DATE_PATTERNS:
        m = rx.match(t)
        if not m:
            continue
        parts = dict(zip(order, m.groups(), strict=True))
        y = int(parts.get("y") or 2000 + int(parts["y2"]))
        mo, d = int(parts["m"]), int(parts["d"])
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _strip_label(token: str) -> str:
    """``"#INV-2231"`` -> ``"INV-2231"``; ``"Date:10/10/2026"`` -> ``"10/10/2026"``."""
    return token.rsplit(":", 1)[-1].lstrip("#").strip()


def _ref(ids: list[int]) -> dict[str, Any]:
    return {"word_ids": ids, "fallback_box": None}


def _text(value: str | None, conf: float, ids: list[int]) -> dict[str, Any]:
    return {"value": value, "confidence": conf if value is not None else 0.0, "source": _ref(ids)}


def _money(cents: int | None, conf: float, ids: list[int]) -> dict[str, Any]:
    return {
        "value_cents": cents,
        "currency": "USD",
        "confidence": conf if cents is not None else 0.0,
        "source": _ref(ids),
    }


def _date(iso: str | None, conf: float, ids: list[int]) -> dict[str, Any]:
    return {"value_iso": iso, "confidence": conf if iso else 0.0, "source": _ref(ids)}


def _row_text(row: list[OfflineWord]) -> str:
    return " ".join(w.text for w in row)


def heuristic_extraction(user_text: str, extra_text: str = "") -> dict[str, Any]:  # noqa: PLR0915
    """Build an ``InvoiceExtraction``-shaped dict from the OCR word list in ``user_text``."""
    words = _parse_words(user_text)
    rows = _rows(words)
    low = [[w.text.casefold().strip(":#.") for w in r] for r in rows]

    out: dict[str, Any] = {
        "doc_type": "invoice",
        "vendor_name": _text(None, 0, []),
        "vendor_address": _text(None, 0, []),
        "vendor_tax_id_last4": _text(None, 0, []),
        "invoice_number": _text(None, 0, []),
        "invoice_date": _date(None, 0, []),
        "due_date": _date(None, 0, []),
        "work_order_number": _text(None, 0, []),
        "property_or_unit": _text(None, 0, []),
        "line_items": [],
        "subtotal": _money(None, 0, []),
        "tax": _money(None, 0, []),
        "total": _money(None, 0, []),
        "remit_to_bank_details": _text(None, 0, []),
        "handwriting_present": False,
        "legibility_issues": [],
        "embedded_instructions": [],
    }
    if not rows:
        out["legibility_issues"] = ["no OCR words recognized"]
        return out

    first = [w for w in rows[0] if not _MONEY_RE.match(w.text)]
    if first:
        out["vendor_name"] = _text(_row_text(first), 0.7, [w.id for w in first])
    if len(rows) > 1:
        out["vendor_address"] = _text(_row_text(rows[1]), 0.5, [w.id for w in rows[1]])

    subtotal_row = len(rows)
    for ri, (row, lrow) in enumerate(zip(rows, low, strict=True)):
        text = _row_text(row)
        for wi, w in enumerate(row):
            core = _strip_label(w.text)
            if _WO_RE.match(core) and out["work_order_number"]["value"] is None:
                out["work_order_number"] = _text(core, 0.9, [w.id])
            elif (
                _INV_RE.match(core)
                and out["invoice_number"]["value"] is None
                and (
                    core.upper().startswith("INV")
                    or any(k in lrow for k in ("invoice", "inv", "no", "number"))
                )
            ):
                out["invoice_number"] = _text(core, 0.85, [w.id])
            if lrow[wi] in ("unit", "apt", "suite") and wi + 1 < len(row):
                nxt = row[wi + 1]
                if out["property_or_unit"]["value"] is None:
                    out["property_or_unit"] = _text(nxt.text.strip(",."), 0.8, [nxt.id])
            iso = _date_iso(core)
            if iso:
                if any(t.startswith("due") for t in lrow) and out["due_date"]["value_iso"] is None:
                    out["due_date"] = _date(iso, 0.85, [w.id])
                elif out["invoice_date"]["value_iso"] is None:
                    out["invoice_date"] = _date(iso, 0.85, [w.id])
        monies = [w for w in row if _MONEY_RE.match(w.text)]
        if monies:
            last = monies[-1]
            if "subtotal" in lrow:
                out["subtotal"] = _money(_money_cents(last.text), 0.85, [last.id])
                subtotal_row = min(subtotal_row, ri)
            elif "tax" in lrow:
                out["tax"] = _money(_money_cents(last.text), 0.8, [last.id])
                subtotal_row = min(subtotal_row, ri)
            elif "total" in lrow:
                out["total"] = _money(_money_cents(last.text), 0.85, [last.id])
                subtotal_row = min(subtotal_row, ri)
        if _BANK_RE.search(text) and out["remit_to_bank_details"]["value"] is None:
            out["remit_to_bank_details"] = _text(text, 0.6, [w.id for w in row])
        if _INSTRUCTION_RE.search(text):
            out["embedded_instructions"].append(text)

    for row in rows[:subtotal_row]:
        monies = [w for w in row if _MONEY_RE.match(w.text)]
        if len(monies) < 2:
            continue
        desc = [w for w in row if w.box[0] < monies[0].box[0] and not _NUM_RE.match(w.text)]
        nums = [w for w in row if _NUM_RE.match(w.text) and w not in monies]
        qty = nums[-1] if nums else None
        out["line_items"].append(
            {
                "description": _text(_row_text(desc) or None, 0.7, [w.id for w in desc]),
                "quantity": _text(qty.text if qty else None, 0.7, [qty.id] if qty else []),
                "unit_price": _money(_money_cents(monies[-2].text), 0.7, [monies[-2].id]),
                "amount": _money(_money_cents(monies[-1].text), 0.75, [monies[-1].id]),
            }
        )

    if out["total"]["value_cents"] is None:
        # no "Total" label: the largest printed amount, at low confidence
        amounts = [(c, w) for w in words if _MONEY_RE.match(w.text) for c in [_money_cents(w.text)]]
        valid = [(c, w) for c, w in amounts if c is not None]
        if valid:
            cents, w = max(valid, key=lambda cw: cw[0])
            out["total"] = _money(cents, 0.5, [w.id])

    for line in extra_text.splitlines():
        if _INSTRUCTION_RE.search(line) and line.strip() not in out["embedded_instructions"]:
            out["embedded_instructions"].append(line.strip())
    return out


def _classifier(text: str) -> dict[str, Any]:
    cats: list[str] = []
    spans: list[dict[str, str]] = []
    for line in text.splitlines():
        if _BANK_RE.search(line) and re.search(r"\b(new|update|change|changed)\b", line, re.I):
            cats.append("bank_or_remit_change")
            spans.append({"category": "bank_or_remit_change", "text": line.strip()})
        elif _INSTRUCTION_RE.search(line):
            cats.append("instruction_to_ai")
            spans.append({"category": "instruction_to_ai", "text": line.strip()})
    if not cats:
        return {"verdict": "clean", "categories": ["none"], "spans": []}
    verdict = "malicious" if "instruction_to_ai" in cats else "suspicious"
    return {"verdict": verdict, "categories": sorted(set(cats)), "spans": spans}


def heuristic_responder(request: LLMRequest) -> dict[str, Any] | None:
    texts = [b.text for b in request.user if isinstance(b, TextBlock)]
    ocr = next((t for t in texts if t.startswith("OCR words:")), "")
    rest = "\n".join(t for t in texts if not t.startswith("OCR words:"))
    if request.role in (ModelRole.EXTRACT_T1, ModelRole.EXTRACT_T2, ModelRole.ADJUDICATE_T3):
        return heuristic_extraction(ocr, rest)
    if request.role == ModelRole.INJECTION_CLASSIFIER:
        return _classifier("\n".join(texts))
    if request.role == ModelRole.WORDING:
        return None  # refusal -> callers fall back to their deterministic templates
    return None


def offline_llm() -> FakeLLM:
    return FakeLLM(heuristic_responder)
