"""Truth-first sampler: work order -> lines -> tax -> totals -> planted defects (DESIGN §6.2).

Every random choice comes from one ``random.Random(seed)``, so the same seed always yields the
same truths. A collision registry (seeded with the historical bills) keeps unintended R4
duplicates out of a set: no two invoices share vendor + invoice number, or vendor + total within
1 % + dates within 7 days, unless a case plants that duplicate on purpose.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from invoicedesk.money import apply_bps

from invoicedesk_data.generator.truth import (
    ExpectedOutcome,
    InvoiceTruth,
    Medium,
    Severity,
    TruthLine,
    expected_from_rules,
)
from invoicedesk_data.universe import (
    AS_OF,
    Universe,
    VendorRec,
    WorkOrderRec,
)

__all__ = [
    "AMOUNT_CAP_CENTS",
    "DEFECTS",
    "NEW_VENDOR_NAMES",
    "CollisionRegistry",
    "TruthSampler",
    "invoice_key",
]

AMOUNT_CAP_CENTS = 500_000
_FUZZY_BPS = 100
_FUZZY_DAYS = 7

#: Planted defect -> the rule failures it must cause (DESIGN §5.5).
DEFECTS: dict[str, dict[str, Severity]] = {
    "over_estimate": {"R3": "review"},
    "wrong_unit": {"R2": "review"},
    "unknown_wo": {"R2": "review", "R3": "review"},
    "missing_wo": {"R2": "review", "R3": "review", "R9": "review"},
    "closed_wo": {"R2": "review"},
    "arithmetic": {"R5": "review"},
    "stale_date": {"R8": "review"},
    "due_before_invoice": {"R8": "review"},
    "over_cap": {"R7": "review"},
    "new_vendor": {"R1": "review", "R2": "review"},
    "pending_vendor": {"R1": "review"},
    "illegible_total": {"R9": "review"},
}

#: Fictional vendors that are NOT in the master (new-vendor cases). Each is far (edit distance
#: and token score) from every master vendor, so R1 sees "new", never "lookalike".
NEW_VENDOR_NAMES: tuple[tuple[str, str], ...] = (
    ("Marigold Mobile Welding", "47 Tannery Lane, Springfield, OR 97478"),
    ("Tidewater Sealcoat Partners", "910 Quarry Road, Springfield, OR 97477"),
    ("Harborlight Signage", "2205 Foundry Street, Springfield, OR 97475"),
    ("Quartzline Countertops", "66 Millrace Court, Springfield, OR 97478"),
    ("Bramblewood Fireproofing", "1380 Kiln Avenue, Springfield, OR 97477"),
)

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def invoice_key(value: str | None) -> str | None:
    """Same normalization as R4 (``INV-002231`` == ``2231``)."""
    if value is None:
        return None
    key = "".join(c for c in value.upper() if c.isalnum())
    for prefix in ("INVOICE", "INV", "NO", "NUMBER"):
        while key.startswith(prefix):
            key = key[len(prefix) :]
    return key.lstrip("0") or None


@dataclass
class _Key:
    vendor_id: str
    number: str | None
    total: int | None
    date: dt.date | None
    ref: str


class CollisionRegistry:
    """Detects unintended R4 duplicates (exact, tweaked-amount and fuzzy)."""

    def __init__(self) -> None:
        self.keys: list[_Key] = []

    def add(
        self,
        vendor_id: str | None,
        number: str | None,
        total: int | None,
        date: dt.date | None,
        ref: str,
    ) -> None:
        if vendor_id is None:
            return
        self.keys.append(_Key(vendor_id, invoice_key(number), total, date, ref))

    def add_truth(self, t: InvoiceTruth) -> None:
        self.add(t.vendor_id, t.invoice_number, t.total_cents, t.invoice_date, t.doc_id)

    def collides(self, t: InvoiceTruth) -> str | None:
        if t.vendor_id is None:
            return None
        num = invoice_key(t.invoice_number)
        for k in self.keys:
            if k.vendor_id != t.vendor_id:
                continue
            if num is not None and k.number == num:
                return k.ref
            if (
                t.total_cents is not None
                and k.total is not None
                and t.invoice_date is not None
                and k.date is not None
                and abs(t.total_cents - k.total) <= abs(apply_bps(k.total, _FUZZY_BPS))
                and abs((t.invoice_date - k.date).days) <= _FUZZY_DAYS
            ):
                return k.ref
        return None


def fmt_date(d: dt.date, style: str) -> str:
    if style == "us":
        return f"{d.month:02d}/{d.day:02d}/{d.year}"
    if style == "us_short":
        return f"{d.month}/{d.day}/{d.year % 100:02d}"
    if style == "long":
        return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"
    if style == "abbr":
        return f"{_MONTHS[d.month - 1][:3]} {d.day}, {d.year}"
    if style == "dmy":
        return f"{d.day} {_MONTHS[d.month - 1][:3]} {d.year}"
    return d.isoformat()


class TruthSampler:
    """Samples :class:`InvoiceTruth` objects for one set (synthetic-core, adversarial, demo)."""

    def __init__(
        self,
        universe: Universe,
        *,
        seed: int,
        set_name: str,
        prefix: str,
        window: tuple[dt.date, dt.date],
        number_offset: int,
        registry: CollisionRegistry | None = None,
    ) -> None:
        self.u = universe
        self.rng = random.Random(seed)  # noqa: S311 - reproducible synthetic data
        self.set_name = set_name
        self.prefix = prefix
        self.window = window
        self.number_offset = number_offset
        self.counters: dict[str, int] = {}
        self.used_wos: set[str] = set()
        self.n = 0
        self.registry = registry or CollisionRegistry()
        if registry is None:
            for h in universe.historical_bills:
                self.registry.add(
                    h.vendor_id, h.invoice_number, h.total_cents, h.invoice_date, h.id
                )

    # ------------------------------------------------------------------ helpers
    def next_id(self) -> str:
        self.n += 1
        return f"{self.prefix}-{self.n:03d}"

    def next_number(self, v: VendorRec) -> str:
        n = self.counters.get(v.slug, v.number_base + self.number_offset)
        self.counters[v.slug] = n + 1
        return v.number_format.format(n=n)

    def rand_date(self, lo: dt.date | None = None, hi: dt.date | None = None) -> dt.date:
        lo = lo or self.window[0]
        hi = hi or self.window[1]
        return lo + dt.timedelta(days=self.rng.randint(0, (hi - lo).days))

    def eligible_wos(
        self, *, vendor: VendorRec | None = None, status: str = "open", large: bool = False
    ) -> list[WorkOrderRec]:
        out = []
        for w in self.u.work_orders:
            if w.status != status or w.wo_number in ("WO-1187", "WO-1179"):
                continue
            if vendor is not None and w.vendor_id != vendor.id:
                continue
            if large != (w.approved_estimate_cents > 450_000):
                continue
            if not large and w.approved_estimate_cents > 380_000:
                continue
            out.append(w)
        return out

    def pick_vendor_wo(
        self,
        *,
        statuses: Iterable[str] = ("approved",),
        exclude: set[str] | None = None,
        wo_status: str = "open",
        large: bool = False,
    ) -> tuple[VendorRec, WorkOrderRec]:
        exclude = exclude or set()
        allowed = set(statuses)
        cands = [
            w
            for w in self.eligible_wos(status=wo_status, large=large)
            if w.wo_number not in self.used_wos
            and self.u.vendor_by_id(w.vendor_id).status in allowed
            and self.u.vendor_by_id(w.vendor_id).slug not in exclude
        ]
        if not cands:
            cands = [
                w
                for w in self.eligible_wos(status=wo_status, large=large)
                if self.u.vendor_by_id(w.vendor_id).status in allowed
            ]
        wo = self.rng.choice(cands)
        self.used_wos.add(wo.wo_number)
        return self.u.vendor_by_id(wo.vendor_id), wo

    def unit_text(self, wo: WorkOrderRec) -> str:
        unit = self.u.unit_by_id(wo.unit_id)
        prop = self.u.property_of_unit(wo.unit_id)
        style = self.rng.randrange(5)
        if style == 0:
            return f"Unit {unit.label}"
        if style == 1:
            return f"Unit {unit.label}, {prop.name}"
        if style == 2:
            return f"{prop.name} #{unit.label}"
        if style == 3:
            return f"Apt {unit.label}"
        return unit.label

    def wo_printed(self, wo_number: str) -> str:
        n = wo_number.split("-")[1]
        return self.rng.choice((wo_number, f"WO #{n}", f"WO {n}", wo_number))

    def lines_from_estimate(self, wo: WorkOrderRec, *, extras: bool = True) -> list[TruthLine]:
        lines: list[TruthLine] = []
        for e in wo.estimate_lines:
            qty = e.quantity
            unit_price = e.amount_cents // qty
            desc = e.description
            if e.unit == "hour" and "(" in desc:
                desc = desc.split(" (")[0]
            lines.append(
                TruthLine(
                    description=desc,
                    quantity=str(qty),
                    unit_price_cents=unit_price,
                    amount_cents=unit_price * qty,
                )
            )
        if extras and self.rng.random() < 0.35:
            cap = min(apply_bps(wo.approved_estimate_cents, 800), 9_000)
            if cap >= 1_500:
                amt = self.rng.randint(15, cap // 100) * 100
                lines.append(
                    TruthLine(
                        description="Materials and supplies",
                        quantity="1",
                        unit_price_cents=amt,
                        amount_cents=amt,
                    )
                )
        return lines

    # ------------------------------------------------------------------ build
    def build(
        self,
        *,
        medium: Medium,
        vendor: VendorRec | None,
        wo: WorkOrderRec | None,
        lines: list[TruthLine],
        rules: dict[str, Severity] | None = None,
        defects: list[str] | None = None,
        template: str | None = None,
        doc_id: str | None = None,
        invoice_number: str | None = None,
        invoice_date: dt.date | None = None,
        due_days: int | None = None,
        vendor_name: str | None = None,
        vendor_address: str | None = None,
        tax_id: str | None = None,
        unit_text: str | None = None,
        wo_number: str | None = "auto",
        remit: str | None = "auto",
        total_delta: int = 0,
        expected: ExpectedOutcome | None = None,
        params: dict[str, Any] | None = None,
        tax_cents: int | None = None,
        check_collisions: bool = True,
        **extra: Any,
    ) -> InvoiceTruth:
        tmpl = template or ("carbon_copy" if medium == "handwritten" else None)
        tmpl = tmpl or (
            vendor.template if vendor else self.rng.choice(("word", "modern", "compact"))
        )
        subtotal = sum(ln.amount_cents for ln in lines)
        rate = vendor.tax_rate_bps if vendor else self.rng.choice((0, 725))
        tax = apply_bps(subtotal, rate) if tax_cents is None else tax_cents
        total = subtotal + tax + total_delta
        date = invoice_date or self.rand_date()
        terms = due_days if due_days is not None else (vendor.payment_terms_days if vendor else 30)
        due = None if tmpl in ("carbon_copy", "compact") else date + dt.timedelta(days=terms)
        if wo_number == "auto":
            wo_number = wo.wo_number if wo else None
        printed_wo = self.wo_printed(wo_number) if wo_number else None
        if remit == "auto":
            remit = self.u.remit_text_for(vendor) if vendor and vendor.prints_remit else None
        rules = dict(rules or {})
        if total > AMOUNT_CAP_CENTS:
            rules.setdefault("R7", "review")
        truth = InvoiceTruth(
            doc_id=doc_id or self.next_id(),
            set_name=self.set_name,
            medium=medium,
            template=tmpl,
            accent=vendor.accent if vendor else "#444444",
            vendor_slug=vendor.slug if vendor else None,
            vendor_id=vendor.id if vendor else None,
            vendor_name=vendor_name or (vendor.legal_name if vendor else "Unknown Vendor"),
            vendor_address=vendor_address or (vendor.address if vendor else None),
            vendor_tax_id_last4=tax_id
            if tax_id is not None
            else (vendor.tax_id_last4 if vendor and vendor.prints_tax_id else None),
            vendor_phone=vendor.phone_on_file if vendor else "(541) 555-0199",
            vendor_email=vendor.email if vendor else None,
            invoice_number=invoice_number or (self.next_number(vendor) if vendor else None),
            invoice_date=date,
            due_date=due,
            work_order_number=wo_number,
            work_order_printed=printed_wo,
            property_or_unit=unit_text
            if unit_text is not None
            else (self.unit_text(wo) if wo else None),
            lines=lines,
            subtotal_cents=subtotal,
            tax_cents=tax,
            tax_rate_bps=rate,
            total_cents=total,
            remit_text=remit,
            handwriting_present=medium == "handwritten",
            defects=list(defects or []),
            expected=expected or expected_from_rules(rules),
            params=dict(params or {}),
            **extra,
        )
        if check_collisions:
            for _ in range(20):
                hit = self.registry.collides(truth)
                if hit is None:
                    break
                truth = truth.model_copy(
                    update={
                        "invoice_date": self.rand_date(),
                        "invoice_number": (
                            self.next_number(vendor)
                            if vendor and invoice_number is None
                            else truth.invoice_number
                        ),
                    }
                )
                if truth.due_date is not None and truth.invoice_date is not None:
                    truth = truth.model_copy(
                        update={"due_date": truth.invoice_date + dt.timedelta(days=terms)}
                    )
            else:  # pragma: no cover - 20 failed resamples would be a seed bug
                raise RuntimeError(f"could not avoid a duplicate collision for {truth.doc_id}")
        self.registry.add_truth(truth)
        return truth

    def clean(self, medium: Medium, **kw: Any) -> InvoiceTruth:
        exclude = kw.pop("exclude", None)
        vendor, wo = self.pick_vendor_wo(exclude=exclude)
        return self.build(
            medium=medium, vendor=vendor, wo=wo, lines=self.lines_from_estimate(wo), **kw
        )

    # ------------------------------------------------------------------ defects
    def with_defect(self, defect: str, medium: Medium, **kw: Any) -> InvoiceTruth:
        rules = dict(DEFECTS[defect])
        common: dict[str, Any] = {"medium": medium, "rules": rules, "defects": [defect], **kw}
        if defect == "over_estimate":
            vendor, wo = self.pick_vendor_wo()
            lines = self.lines_from_estimate(wo, extras=False)
            est = wo.approved_estimate_cents
            allowed = max(apply_bps(est, 1000), 10_000)
            extra = allowed + self.rng.randint(5, 40) * 1_000
            lines.append(
                TruthLine(
                    description="Additional work not in estimate",
                    quantity="1",
                    unit_price_cents=extra,
                    amount_cents=extra,
                )
            )
            return self.build(vendor=vendor, wo=wo, lines=lines, **common)
        if defect == "wrong_unit":
            vendor, wo = self.pick_vendor_wo()
            prop_id = self.u.unit_by_id(wo.unit_id).property_id
            others = [u for u in self.u.units if u.property_id == prop_id and u.id != wo.unit_id]
            return self.build(
                vendor=vendor,
                wo=wo,
                lines=self.lines_from_estimate(wo),
                unit_text=f"Unit {self.rng.choice(others).label}",
                **common,
            )
        if defect == "unknown_wo":
            vendor, wo = self.pick_vendor_wo()
            return self.build(
                vendor=vendor,
                wo=wo,
                lines=self.lines_from_estimate(wo),
                wo_number=f"WO-{self.rng.randint(1900, 1999)}",
                **common,
            )
        if defect == "missing_wo":
            vendor, wo = self.pick_vendor_wo()
            return self.build(
                vendor=vendor, wo=wo, lines=self.lines_from_estimate(wo), wo_number=None, **common
            )
        if defect == "closed_wo":
            vendor, wo = self.pick_vendor_wo(wo_status="closed")
            return self.build(vendor=vendor, wo=wo, lines=self.lines_from_estimate(wo), **common)
        if defect == "arithmetic":
            vendor, wo = self.pick_vendor_wo()
            delta = self.rng.choice((-1, 1)) * self.rng.randint(10, 95) * 100
            return self.build(
                vendor=vendor,
                wo=wo,
                lines=self.lines_from_estimate(wo),
                total_delta=delta,
                **common,
            )
        if defect == "stale_date":
            vendor, wo = self.pick_vendor_wo()
            d = AS_OF - dt.timedelta(days=self.rng.randint(100, 150))
            return self.build(
                vendor=vendor, wo=wo, lines=self.lines_from_estimate(wo), invoice_date=d, **common
            )
        if defect == "due_before_invoice":
            vendor, wo = self.pick_vendor_wo()
            if common.get("template") is None and vendor.template in ("carbon_copy", "compact"):
                common["template"] = "word"
            return self.build(
                vendor=vendor,
                wo=wo,
                lines=self.lines_from_estimate(wo),
                due_days=-self.rng.randint(5, 20),
                **common,
            )
        if defect == "over_cap":
            vendor, wo = self.pick_vendor_wo(large=True)
            return self.build(
                vendor=vendor, wo=wo, lines=self.lines_from_estimate(wo, extras=False), **common
            )
        if defect == "new_vendor":
            name, address = NEW_VENDOR_NAMES[self.rng.randrange(len(NEW_VENDOR_NAMES))]
            _, wo = self.pick_vendor_wo()
            return self.build(
                vendor=None,
                wo=wo,
                lines=self.lines_from_estimate(wo),
                vendor_name=name,
                vendor_address=address,
                invoice_number=f"{self.rng.randint(100, 999)}",
                tax_id=f"{self.rng.randint(0, 9999):04d}",
                remit=None,
                **common,
            )
        if defect == "pending_vendor":
            vendor, wo = self.pick_vendor_wo(statuses=("pending",))
            return self.build(vendor=vendor, wo=wo, lines=self.lines_from_estimate(wo), **common)
        if defect == "illegible_total":
            vendor, wo = self.pick_vendor_wo()
            return self.build(
                vendor=vendor,
                wo=wo,
                lines=self.lines_from_estimate(wo),
                illegible_fields=["total"],
                **common,
            )
        raise KeyError(defect)
