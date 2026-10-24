"""Builds the web app's mock-mode fixtures from the synthetic demo batch.

Reads ``data/demo/`` (the 25 demo files and expected.json) and ``data/gold/demo/`` (gold
extractions, boxes and hidden-text boxes), then writes:

- ``apps/web/public/mock/pages/<nn>.jpg``: page 1 at the processed size (1,600 px long edge)
- ``apps/web/public/mock/thumbs/<nn>.jpg``: 240 px thumbnails
- ``apps/web/public/mock/report-card.pdf``: a clearly-labelled sample Report Card
- ``apps/web/lib/mock/fixtures/demo-batch.json``: DocumentDetail objects shaped exactly like
  docs/api-contract.md, plus dashboard, comparison, follow-up and scorecard samples

All data is synthetic (Maple Row). Bank and account numbers are masked to the last 4 here, so the
fixtures never carry a full number. The calibrated confidences, costs and scorecard values are
illustrative samples for offline UI work, not measurements; the UI labels mock mode as such.

Run from the repo root:  uv run python apps/web/scripts/build_mock_fixtures.py
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymupdf as fitz
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "apps" / "web"
DEMO = ROOT / "data" / "demo"
GOLD = ROOT / "data" / "gold" / "demo"
OUT_PAGES = WEB / "public" / "mock" / "pages"
OUT_THUMBS = WEB / "public" / "mock" / "thumbs"
OUT_JSON = WEB / "lib" / "mock" / "fixtures" / "demo-batch.json"

NS = uuid.UUID("6c1b4f0e-2f0a-4b53-9a49-1f3d0c6a7e21")
BATCH_ID = str(uuid.uuid5(NS, "demo-batch"))
TENANT = "maple_row"
UPLOADER_EMAIL = "dana.clerk@maplerow.example"
TAU_FIELD = 0.90
TS = "2026-10-20T14:20:00+00:00"

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"

HEADER = [
    "vendor_name",
    "vendor_address",
    "vendor_tax_id_last4",
    "invoice_number",
    "invoice_date",
    "due_date",
    "work_order_number",
    "property_or_unit",
    "subtotal",
    "tax",
    "total",
    "remit_to_bank_details",
]
MONEY = {"subtotal", "tax", "total"}
DATES = {"invoice_date", "due_date"}
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

RULES = [
    ("R1", "vendor_known"),
    ("R2", "work_order_exists"),
    ("R3", "three_way_match"),
    ("R4", "duplicate"),
    ("R5", "arithmetic"),
    ("R6", "bank_change"),
    ("R7", "amount_cap"),
    ("R8", "dates"),
    ("R9", "confidence"),
    ("R10", "injection"),
]


def staff_id(email: str) -> str:
    return str(uuid.uuid5(NS, f"staff:{email}"))


def doc_id(n: int) -> str:
    return str(uuid.uuid5(NS, f"demo-doc-{n:02d}"))


def mask_text(text: str) -> str:
    """Masks every run of 6+ digits (allowing spaces/dashes) to bullets plus the last 4."""

    def repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "••••" + digits[-4:]

    return re.sub(r"\d(?:[ -]?\d){5,}", repl, text)


def fmt_cents(c: int | None) -> str | None:
    if c is None:
        return None
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def fmt_date(iso: str | None) -> str | None:
    if not iso:
        return None
    y, m, d = iso.split("-")
    return f"{MONTHS[int(m) - 1]} {int(d)}, {y}"


def stable_unit(seed: str) -> float:
    """Deterministic number in [0, 1) for illustrative jitter."""
    return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def render_page(src: Path) -> Image.Image:
    if src.suffix.lower() == ".pdf":
        with fitz.open(src) as pdf:
            page = pdf[0]
            scale = 1600 / max(page.rect.width, page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    img = Image.open(src).convert("RGB")
    if max(img.size) != 1600:
        s = 1600 / max(img.size)
        img = img.resize((round(img.width * s), round(img.height * s)), Image.Resampling.LANCZOS)
    return img


def save_images(n: int, src: Path) -> tuple[int, int]:
    img = render_page(src)
    OUT_PAGES.mkdir(parents=True, exist_ok=True)
    OUT_THUMBS.mkdir(parents=True, exist_ok=True)
    img.save(OUT_PAGES / f"{n:02d}.jpg", "JPEG", quality=62, optimize=True, progressive=True)
    t = img.copy()
    t.thumbnail((240, 320))
    t.save(OUT_THUMBS / f"{n:02d}.jpg", "JPEG", quality=70, optimize=True)
    return img.width, img.height


def field_value(ext: dict[str, Any], name: str) -> tuple[str | None, str | None, float]:
    f = ext.get(name)
    if not isinstance(f, dict):
        return None, None, 0.0
    conf = float(f.get("confidence") or 0.0)
    if name in MONEY:
        v = f.get("value_cents")
        return (str(v) if v is not None else None), fmt_cents(v), conf
    if name in DATES:
        v = f.get("value_iso")
        return v, fmt_date(v), conf
    v = f.get("value")
    if v is None:
        return None, None, conf
    if name == "remit_to_bank_details":
        v = mask_text(str(v))
    return str(v), str(v), conf


RULE_FIELDS = {
    "R1": ["vendor_name"],
    "R2": ["work_order_number", "property_or_unit"],
    "R3": ["total"],
    "R4": ["invoice_number", "total"],
    "R5": ["subtotal", "tax", "total"],
    "R6": ["remit_to_bank_details"],
    "R7": ["total"],
    "R8": ["invoice_date"],
    "R9": [],
    "R10": [],
}

BADGE_FOR = {
    "R1": "new_vendor",
    "R2": "work_order",
    "R3": "over_estimate",
    "R4": "duplicate",
    "R6": "bank_change",
    "R7": "over_cap",
    "R9": "low_confidence",
    "R10": "injection",
}


def pass_message(rid: str, t: dict[str, Any]) -> str:
    vendor = t.get("vendor_name") or "Vendor"
    wo = t.get("work_order_number") or "the work order"
    return {
        "R1": f"Vendor matched: {vendor}.",
        "R2": f"Work order {wo} is open and matches {t.get('property_or_unit') or 'the unit'}.",
        "R3": f"Within the approved estimate for {wo}.",
        "R4": "No earlier bill with this vendor, number or amount.",
        "R5": "Line items, tax and total add up.",
        "R6": "Bank details match the vendor on file.",
        "R7": "Under the $5,000.00 approval limit.",
        "R8": "Invoice date is recent and not in the future.",
        "R9": "Every required field was read with high confidence.",
        "R10": "No hidden instructions found.",
    }[rid]


def build_doc(n: int, f: dict[str, Any], gold: dict[str, Any], size: tuple[int, int]) -> dict[str, Any]:
    t = gold["truth"]
    exp = gold["expected"]
    ext = gold["extraction"]
    status = exp["status"]
    quarantined = bool(exp.get("quarantined"))
    failing: dict[str, str] = exp.get("rules", {})
    illegible = set(t.get("illegible_fields") or [])
    did = doc_id(n)

    fields: list[dict[str, Any]] = []
    if not quarantined:
        for name in HEADER:
            value, display, raw = field_value(ext, name)
            if value is None and name not in illegible:
                continue
            box_info = gold["boxes"].get(name)
            failed_rule = next((r for r in failing if name in RULE_FIELDS[r]), None)
            jitter = stable_unit(f"{did}:{name}")
            if name in illegible:
                cal, raw = 0.31 + 0.1 * jitter, 0.62
                value, display = None, None
            else:
                cal = round(0.93 + 0.06 * jitter, 2)
            if failed_rule or name in illegible or cal < 0.5:
                band = "red"
            elif cal >= TAU_FIELD:
                band = "green"
            else:
                band = "amber"
            reason = None
            if name in illegible:
                reason = f"{name.replace('_', ' ').capitalize()} unreadable: please confirm"
                band = "amber" if name == "total" else "red"
            elif failed_rule:
                reason = exp["headline"]
            fields.append(
                {
                    "name": name,
                    "value": value,
                    "display_value": display,
                    "raw_conf": round(raw, 2),
                    "calibrated_conf": round(cal, 2),
                    "band": band,
                    "page": 0 if box_info else None,
                    "box": box_info["box"] if box_info else None,
                    "word_ids": [],
                    "reason": reason,
                }
            )

    line_items = []
    if not quarantined:
        for i, li in enumerate(ext.get("line_items") or []):
            amount = (li.get("amount") or {}).get("value_cents")
            unit = (li.get("unit_price") or {}).get("value_cents")
            box = gold["boxes"].get(f"line_items.{i}.description")
            line_items.append(
                {
                    "description": (li.get("description") or {}).get("value"),
                    "quantity": (li.get("quantity") or {}).get("value"),
                    "unit_price_cents": unit,
                    "amount_cents": amount,
                    "box": box["box"] if box else None,
                    "page": 0 if box else None,
                }
            )

    rule_results = []
    if not quarantined:
        for rid, rname in RULES:
            sev = failing.get(rid)
            if sev:
                msg = exp["headline"]
                if rid == "R2" and "R1" in failing:
                    msg = f"Work order {t.get('work_order_number')} is not assigned to this vendor."
                rule_results.append(
                    {
                        "rule_id": rid,
                        "name": rname,
                        "passed": False,
                        "severity": sev,
                        "message": msg,
                        "fields": RULE_FIELDS[rid] or sorted(illegible),
                        "details": {},
                    }
                )
            else:
                rule_results.append(
                    {
                        "rule_id": rid,
                        "name": rname,
                        "passed": True,
                        "severity": "pass",
                        "message": pass_message(rid, t),
                        "fields": [],
                        "details": {},
                    }
                )
    else:
        rule_results.append(
            {
                "rule_id": "R10",
                "name": "injection",
                "passed": False,
                "severity": "flag",
                "message": exp["headline"],
                "fields": [],
                "details": {"categories": exp["categories"]},
            }
        )

    detectors = set(exp.get("detectors") or [])
    hidden_spans = [
        {
            "text": mask_text(h["text"]),
            "reason": "white text on a white background"
            if "hidden_text" in detectors
            else "very faint text, nearly invisible on paper",
            "page": h["page"],
            "box": h["box"],
            "source": "pdf_text" if "hidden_text" in detectors else "ocr_enhanced",
        }
        for h in gold.get("hidden_boxes") or []
    ]
    screen_results = []
    for det in ("hidden_text", "faint_text"):
        hit = det in detectors
        screen_results.append(
            {
                "detector": det,
                "verdict": "malicious" if hit else "clean",
                "categories": exp["categories"] if hit else [],
                "spans": hidden_spans if hit else [],
            }
        )
    screen_results.append(
        {
            "detector": "injection_classifier",
            "verdict": "malicious" if quarantined else "clean",
            "categories": exp["categories"] if quarantined else [],
            "spans": hidden_spans if quarantined else [],
        }
    )

    medium = f["medium"]
    if quarantined:
        extractions = [
            {"tier": 0, "model_id": HAIKU, "cost_usd": "0.0011", "latency_ms": 640, "refused": False, "cache_read_tokens": 1536}
        ]
    elif medium == "handwritten" and illegible:
        extractions = [
            {"tier": 1, "model_id": HAIKU, "cost_usd": "0.0052", "latency_ms": 2100, "refused": False, "cache_read_tokens": 2048},
            {"tier": 2, "model_id": SONNET, "cost_usd": "0.0229", "latency_ms": 5900, "refused": False, "cache_read_tokens": 2048},
        ]
    else:
        extractions = [
            {"tier": 1, "model_id": HAIKU, "cost_usd": "0.0101", "latency_ms": 2300 + int(900 * stable_unit(did)), "refused": False, "cache_read_tokens": 2048}
        ]
    cost = sum((Decimal(e["cost_usd"]) for e in extractions), Decimal(0))

    badges = sorted({BADGE_FOR[r] for r in failing if r in BADGE_FOR})
    if quarantined:
        badges = sorted({"injection", "bank_change", *(d for d in detectors)})
    if t.get("handwriting_present"):
        badges.append("handwriting")

    approvals_required = None
    if status == "Flagged":
        approvals_required = {"count": 2, "callback": True}

    followups = []
    headline = exp["headline"] or None
    total_cents = None if quarantined or "total" in illegible else t.get("total_cents")
    w, h = size
    return {
        "id": did,
        "batch_id": BATCH_ID,
        "filename": f["filename"],
        "source": "seed",
        "status": status,
        "column": {"ReadyToPay": "ready_to_pay", "NeedsReview": "needs_review", "Flagged": "flagged"}[status],
        "vendor_name": None if quarantined else t.get("vendor_name"),
        "invoice_number": None if quarantined else t.get("invoice_number"),
        "total_cents": total_cents,
        "headline": headline,
        "badges": badges,
        "cost_usd": format(cost.normalize(), "f"),
        "thumbnail_url": f"/mock/thumbs/{n:02d}.jpg",
        "created_at": TS,
        "updated_at": TS,
        "pages": [{"index": 0, "width": w, "height": h, "image_url": f"/mock/pages/{n:02d}.jpg"}],
        "original_url": f"/mock/pages/{n:02d}.jpg",
        "fields": fields,
        "line_items": line_items,
        "rule_results": rule_results,
        "screen_results": screen_results,
        "embedded_instructions": [mask_text(s["text"]) for s in hidden_spans],
        "extractions": extractions,
        "approvals": [],
        "approvals_required": approvals_required,
        "bill": None,
        "followups": followups,
        "email": None,
        "trace_url": None,
        "uploaded_by": staff_id(UPLOADER_EMAIL),
        "redactions": redactions(gold),
        # mock-only hints (ignored by the UI types): used by the mock server to simulate re-routing
        "_mock": {
            "truth_total_cents": t.get("total_cents"),
            "truth_vendor": t.get("vendor_name"),
            "vendor_email": t.get("vendor_email"),
            "medium": medium,
            "failing": failing,
            "illegible": sorted(illegible),
        },
    }


def redactions(gold: dict[str, Any]) -> list[dict[str, Any]]:
    """Page regions printing a bank/account number (requested contract addition)."""
    out = []
    remit = gold["boxes"].get("remit_to_bank_details")
    if remit:
        digits = re.sub(r"\D", "", remit["text"])
        out.append({"page": remit["page"], "box": remit["box"], "label": f"Bank details hidden (acct ••••{digits[-4:]})"})
    return out


def comparison(docs: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    lost = 0
    naive_wrong = 0
    for d in docs:
        m = d["_mock"]
        amount = m["truth_total_cents"]
        failing = m["failing"]
        cents = fmt_cents(amount)
        if d["status"] == "ReadyToPay":
            naive = {"outcome": "posted", "verdict": "correct", "reason": f"Posted {cents}", "amount_cents": amount}
            ours = {"outcome": "ready_to_pay", "verdict": "correct", "reason": "All checks passed"}
        else:
            ours = {"outcome": "held", "verdict": "correct", "reason": d["headline"]}
            if "R10" in failing:
                reason = "Paid to attacker's account"
            elif "R4" in failing:
                reason = f"Posted duplicate {cents}"
            elif "R3" in failing:
                reason = f"Paid {cents}, $340.00 over the approved estimate"
            elif "R1" in failing:
                reason = f"Paid unknown vendor {cents}"
            else:
                reason = f"Guessed an unreadable total and posted {cents}"
            naive = {"outcome": "posted", "verdict": "wrong", "reason": reason, "amount_cents": amount}
            naive_wrong += 1
            if "R3" in failing:
                lost += 34000
            elif amount:
                lost += amount
        rows.append({"document_id": d["id"], "filename": d["filename"], "naive": naive, "invoicedesk": ours})
    return {
        "rows": rows,
        "summary": {"naive_wrong": naive_wrong, "invoicedesk_wrong": 0, "naive_dollars_lost_cents": lost},
    }


def dashboard(docs: list[dict[str, Any]]) -> dict[str, Any]:
    ready = sum(1 for d in docs if d["status"] == "ReadyToPay")
    dup = sum(d["_mock"]["truth_total_cents"] for d in docs if "R4" in d["_mock"]["failing"])
    fraud = sum(d["_mock"]["truth_total_cents"] for d in docs if "R10" in d["_mock"]["failing"])
    over = 34000 * sum(1 for d in docs if "R3" in d["_mock"]["failing"])
    total_cost = sum((Decimal(d["cost_usd"]) for d in docs), Decimal(0))
    per = (total_cost / len(docs)).quantize(Decimal("0.001"))
    return {
        "invoices": len(docs),
        "hands_free_pct": round(100 * ready / len(docs), 1),
        "hours_saved": round(ready * 8 / 60, 1),
        "dollars_caught_cents": {"duplicates": dup, "fraud": fraud, "overcharges": over, "total": dup + fraud + over},
        "cost_per_invoice_usd": format(per.normalize(), "f"),
        "manual_benchmark_per_invoice_cents": 990,
        "assumptions": {"minutes_per_invoice": 8},
        "label": "benchmark on synthetic data",
    }


def scorecard() -> dict[str, Any]:
    # Illustrative sample shaped like GET /scorecard/latest. Not a measurement.
    bins = [
        {"confidence": 0.15, "accuracy": 0.12, "count": 14},
        {"confidence": 0.35, "accuracy": 0.31, "count": 22},
        {"confidence": 0.55, "accuracy": 0.58, "count": 41},
        {"confidence": 0.72, "accuracy": 0.69, "count": 77},
        {"confidence": 0.86, "accuracy": 0.88, "count": 160},
        {"confidence": 0.97, "accuracy": 0.98, "count": 446},
    ]
    return {
        "run_id": str(uuid.uuid5(NS, "sample-run")),
        "git_sha": "sample",
        "config": "D",
        "finished_at": "2026-10-20T03:00:00+00:00",
        "metrics": {
            "field_f1": {"value": 0.97, "ci": [0.955, 0.981]},
            "line_item_f1": {"value": 0.93, "ci": [0.91, 0.95]},
            "stp": {"value": 0.76, "ci": [0.70, 0.81]},
            "false_stp": {"value": 0.0, "ci": [0.0, 0.01]},
            "injection_recall": {"value": 1.0, "ci": [0.9, 1.0]},
            "ece": {"value": 0.031, "ci": [0.022, 0.041]},
            "usd_per_doc": {"value": "0.011"},
            "p50_s": {"value": 4.1},
            "p95_s": {"value": 9.8},
        },
        "label": "benchmark on synthetic data",
        "reliability": bins,
    }


def followups(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in docs:
        if "R3" in d["_mock"]["failing"]:
            out.append(
                {
                    "id": str(uuid.uuid5(NS, f"followup-{d['id']}")),
                    "document_id": d["id"],
                    "to_addr": "billing@rapidrooter.example",
                    "draft_subject": f"Invoice {d['invoice_number']}: amount above approved estimate",
                    "draft_body": (
                        "Hi Rapid Rooter team,\n\n"
                        f"Thanks for invoice {d['invoice_number']}. It comes to $340.00 more than the approved "
                        "estimate for Unit 12B (WO-1187). Could you send a revised invoice, or a short note on the "
                        "extra work so we can approve the change?\n\nThanks,\nMaple Row Property Management"
                    ),
                    "approved_by": None,
                    "sent_at": None,
                }
            )
    return out


def report_card_pdf() -> None:
    out = WEB / "public" / "mock" / "report-card.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((54, 80), "AP Automation Report Card", fontsize=24)
    page.insert_text((54, 110), "SAMPLE: offline mock mode. Not a measured eval run.", fontsize=12, color=(0.7, 0.1, 0.1))
    lines = [
        "Field accuracy        97%   (sample)",
        "Hands-free            76%   (sample)",
        "Cost per invoice      $0.011 (sample)",
        "Median time           4.1 s (sample)",
        "",
        "Run `make report-card` against a real eval run for the actual PDF.",
        "Benchmark on synthetic data.",
    ]
    for i, line in enumerate(lines):
        page.insert_text((54, 160 + 22 * i), line, fontsize=13, fontname="cour")
    doc.save(out, garbage=4, deflate=True)
    doc.close()


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def unmasked_digit_runs(node: Any, key: str = "") -> list[str]:
    """Finds 6+ digit runs in free text. Ids and money ``value`` strings (cents) are exempt."""
    if isinstance(node, dict):
        return [r for k, v in node.items() for r in unmasked_digit_runs(v, k)]
    if isinstance(node, list):
        return [r for v in node for r in unmasked_digit_runs(v, key)]
    if isinstance(node, str):
        if UUID_RE.match(node) or (key == "value" and re.fullmatch(r"-?\d+", node)):
            return []
        return re.findall(r"\d{6,}", node)
    return []


def main() -> int:
    expected = json.loads((DEMO / "expected.json").read_text())
    docs = []
    for n, f in enumerate(expected["files"], start=1):
        gold = json.loads((GOLD / f"{f['gold_id']}.json").read_text())
        size = save_images(n, DEMO / f["filename"])
        docs.append(build_doc(n, f, gold, size))
    payload = {
        "_note": "Generated by apps/web/scripts/build_mock_fixtures.py from data/demo + data/gold/demo. "
        "Synthetic data; confidences, costs and scorecard values are illustrative samples for mock mode.",
        "batch": {"id": BATCH_ID, "label": "25-invoice demo batch", "mode": "replay", "created_at": TS},
        "tenant_id": TENANT,
        "staff": [
            {"id": staff_id("dana.clerk@maplerow.example"), "email": "dana.clerk@maplerow.example", "display_name": "Dana Whitlock", "role": "clerk"},
            {"id": staff_id("priya.approver@maplerow.example"), "email": "priya.approver@maplerow.example", "display_name": "Priya Okafor", "role": "approver"},
            {"id": staff_id("sam.owner@maplerow.example"), "email": "sam.owner@maplerow.example", "display_name": "Sam Marlowe", "role": "owner"},
        ],
        "documents": docs,
        "comparison": comparison(docs),
        "dashboard": dashboard(docs),
        "scorecard": scorecard(),
        "followups": followups(docs),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    report_card_pdf()
    full_numbers = unmasked_digit_runs(payload)
    if full_numbers:
        print(f"refusing: unmasked digit runs in fixtures: {full_numbers[:5]}", file=sys.stderr)
        return 1
    print(f"wrote {len(docs)} documents to {OUT_JSON.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
