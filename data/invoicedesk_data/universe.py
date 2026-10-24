"""The fictional Maple Row business universe (DESIGN §6.2 "Business universe").

6 properties, 40 units, 30 vendors (24 approved, 4 pending, 2 blocked), 160 work orders with
estimate lines and 18 months of historical bills, generated deterministically from a fixed seed
(``random.Random`` plus a seeded Faker instance). Everything here is fictional:

* company names are invented; phone numbers use the reserved 555-01xx range; e-mail domains use
  the reserved ``.example`` TLD; EINs start with ``00`` (never issued);
* bank details use the documented test routing number ``110000000`` and fictional ``000999...``
  account numbers. Only :func:`invoicedesk.remit.remit_fingerprint` and the last 4 digits are
  stored in the vendor master; the full test text is re-derived by :func:`remit_text` when an
  invoice prints remit-to details.

The demo entities are fixed: Rapid Rooter Plumbing LLC, WO-1187 for Unit 12B, WO-1179 for Unit
10A, historical bill INV-2231 ($1,240.00, 2026-10-10) and the lookalike "Rapid Rotor Plumbing
LLC", which is deliberately NOT in the vendor master.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from typing import Literal

from faker import Faker
from invoicedesk.contracts.tenant_data import (
    EstimateLine,
    PriorInvoice,
    Property,
    TenantData,
    Unit,
    Vendor,
    WorkOrder,
)
from invoicedesk.remit import remit_fingerprint
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AS_OF",
    "LOOKALIKE_VENDOR",
    "SEED",
    "TENANT_ID",
    "TENANT_NAME",
    "TEST_ROUTING_NUMBER",
    "CatalogItem",
    "EstimateLineRec",
    "HistoricalBillRec",
    "PropertyRec",
    "StaffRec",
    "UnitRec",
    "Universe",
    "VendorRec",
    "WorkOrderRec",
    "build_universe",
    "remit_text",
    "stable_id",
]

SEED = 20261020
TENANT_ID = "maple_row"
TENANT_NAME = "Maple Row Property Management"
#: The anchor date of the universe. History ends before it; every synthetic set is dated
#: relative to it and is evaluated with ``today = AS_OF`` (R8 is date-relative).
AS_OF = dt.date(2026, 10, 20)
#: Stripe's documented test routing number (not a real bank). Accounts are ``000999...``.
TEST_ROUTING_NUMBER = "110000000"
TEST_BANK_NAME = "Fictional Test Bank (TEST ONLY)"
LOOKALIKE_VENDOR = "Rapid Rotor Plumbing LLC"

_NAMESPACE = uuid.UUID("6f1c7a52-6d1e-4c0e-9a37-3f2b9c1d0a11")


def stable_id(*parts: object) -> str:
    """Deterministic UUID for a seed entity (idempotent re-seeding)."""
    return str(uuid.uuid5(_NAMESPACE, ":".join(str(p) for p in parts)))


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PropertyRec(_M):
    id: str
    slug: str
    name: str
    address: str


class UnitRec(_M):
    id: str
    property_id: str
    label: str


class CatalogItem(_M):
    description: str
    low_cents: int
    high_cents: int
    unit: Literal["each", "hour", "visit", "sqft", "lot"] = "each"


class VendorRec(_M):
    id: str
    slug: str
    legal_name: str
    aliases: tuple[str, ...] = ()
    trade: str
    address: str
    tax_id_last4: str
    remit_to_hash: str
    bank_last4: str
    phone_on_file: str
    email: str
    qbo_vendor_ref: str
    status: Literal["approved", "pending", "blocked"]
    #: Invoice-number format, e.g. ``"INV-{n}"``; ``n`` comes from the per-vendor counter.
    number_format: str
    number_base: int
    tax_rate_bps: int
    payment_terms_days: int
    #: Template the vendor's invoices are rendered with (see generator/templates).
    template: str
    accent: str
    prints_remit: bool
    prints_tax_id: bool


class EstimateLineRec(_M):
    id: str
    description: str
    amount_cents: int
    quantity: int = 1
    unit: str = "each"


class WorkOrderRec(_M):
    id: str
    wo_number: str
    unit_id: str
    vendor_id: str
    scope: str
    status: Literal["open", "closed"]
    approved_estimate_cents: int
    approved_by: str
    approved_at: dt.date
    estimate_lines: tuple[EstimateLineRec, ...]


class HistoricalBillRec(_M):
    id: str
    vendor_id: str
    invoice_number: str
    invoice_date: dt.date
    total_cents: int
    work_order_id: str | None = None


class StaffRec(_M):
    email: str
    display_name: str
    role: Literal["clerk", "approver", "owner"]


# --------------------------------------------------------------------------- static catalog
_PROPERTIES: list[tuple[str, str, list[str]]] = [
    (
        "maple-court",
        "Maple Court Apartments",
        ["10A", "10B", "11A", "11B", "12A", "12B", "14A", "14B"],
    ),
    ("alder-row", "Alder Row Townhomes", ["A1", "A2", "A3", "A4", "A5", "A6"]),
    ("juniper-house", "Juniper House", ["101", "102", "103", "104", "105", "106", "107", "108"]),
    ("willow-bend", "Willow Bend Duplexes", ["1L", "1U", "2L", "2U", "3L", "3U"]),
    ("cedar-flats", "Cedar Flats", ["201", "202", "203", "204", "205", "206", "207"]),
    ("birch-cottages", "Birch Street Cottages", ["C1", "C2", "C3", "C4", "C5"]),
]

# (slug, legal name, aliases, trade, status)
_VENDORS: list[tuple[str, str, tuple[str, ...], str, str]] = [
    (
        "rapid-rooter",
        "Rapid Rooter Plumbing LLC",
        ("Rapid Rooter", "Rapid Rooter Plumbing"),
        "plumbing",
        "approved",
    ),
    ("birchline", "Birchline Electric Co.", ("Birchline Electric",), "electrical", "approved"),
    ("copperleaf", "Copperleaf Heating & Air", ("Copperleaf HVAC",), "hvac", "approved"),
    ("summit-ridge", "Summit Ridge Roofing", (), "roofing", "approved"),
    (
        "brushstroke",
        "Brushstroke Painting Studio",
        ("Brushstroke Painting",),
        "painting",
        "approved",
    ),
    ("greenwhistle", "Greenwhistle Landscaping", (), "landscaping", "approved"),
    ("sparkvale", "Sparkvale Cleaning Services", ("Sparkvale Cleaning",), "cleaning", "approved"),
    ("quillfeather", "Quillfeather Appliance Repair", (), "appliance", "approved"),
    ("tumbler", "Tumbler Lock & Key", (), "locksmith", "approved"),
    ("antwhisper", "Antwhisper Pest Control", (), "pest", "approved"),
    ("oakhollow", "Oakhollow Flooring", (), "flooring", "approved"),
    ("handy-heron", "Handy Heron Home Repair", ("Handy Heron",), "handyman", "approved"),
    ("clearpane", "Clearpane Glass Works", (), "glass", "approved"),
    ("downspout", "Downspout Gutter Pros", (), "gutters", "approved"),
    ("frostline", "Frostline Snow & Ice", (), "snow", "approved"),
    (
        "voltwise",
        "Voltwise Electrical Contractors",
        ("Voltwise Electric",),
        "electrical",
        "approved",
    ),
    ("thistledown", "Thistledown Carpet Care", (), "carpet", "approved"),
    ("stonebridge", "Stonebridge Masonry", (), "masonry", "approved"),
    ("kettle-creek", "Kettle Creek Drain Services", (), "plumbing", "approved"),
    ("mossgate", "Mossgate Tree Care", (), "tree", "approved"),
    ("pinecone", "Pinecone Drywall & Patch", (), "drywall", "approved"),
    ("bluejay", "Bluejay Window Washing", (), "windows", "approved"),
    ("ironwood", "Ironwood Fence & Gate", (), "fencing", "approved"),
    ("ember-hearth", "Ember Hearth Chimney Sweep", (), "chimney", "approved"),
    ("fieldstone", "Fieldstone Paving Co.", (), "paving", "pending"),
    ("nightowl", "Nightowl Emergency Restoration", (), "restoration", "pending"),
    ("larkspur", "Larkspur Junk Haulers", (), "hauling", "pending"),
    ("cobalt-garage", "Cobalt Garage Door Service", (), "garage", "pending"),
    ("shortcut", "Shortcut Contracting Group", (), "handyman", "blocked"),
    ("quickfix", "Quickfix Handyman Express", (), "handyman", "blocked"),
]

TEMPLATES: tuple[str, ...] = (
    "trades",
    "carbon_copy",
    "quickbooks",
    "word",
    "modern",
    "letterhead",
    "compact",
    "statement",
)

_ACCENTS = (
    "#1f4e79",
    "#7a1f1f",
    "#1f6b3a",
    "#5b2a86",
    "#8a5a00",
    "#0f6b6b",
    "#3d3d3d",
    "#a23b72",
    "#2d5f8b",
    "#6b4f1f",
    "#245c4c",
    "#7b2d26",
)

_NUMBER_FORMATS = (
    "INV-{n}",
    "{n}",
    "A-{n}",
    "26-{n:04d}",
    "INV{n:05d}",
    "MR{n}",
    "SR-{n}",
    "{n}-B",
)

_I = CatalogItem
_CATALOG: dict[str, list[CatalogItem]] = {
    "plumbing": [
        _I(description="Replace kitchen drain trap", low_cents=18_000, high_cents=42_000),
        _I(description="Clear main sewer line", low_cents=22_000, high_cents=65_000),
        _I(description="Replace water heater (40 gal)", low_cents=140_000, high_cents=210_000),
        _I(description="Repair leaking supply valve", low_cents=9_000, high_cents=24_000),
        _I(description="Replace toilet wax ring and flange", low_cents=12_000, high_cents=28_000),
        _I(description="Plumbing labor", low_cents=9_500, high_cents=14_500, unit="hour"),
    ],
    "electrical": [
        _I(description="Replace GFCI outlets", low_cents=6_500, high_cents=14_000),
        _I(description="Install LED fixture", low_cents=11_000, high_cents=26_000),
        _I(description="Troubleshoot tripping breaker", low_cents=15_000, high_cents=32_000),
        _I(description="Replace smoke/CO detector", low_cents=7_500, high_cents=13_500),
        _I(description="Electrical labor", low_cents=10_500, high_cents=15_500, unit="hour"),
    ],
    "hvac": [
        _I(description="Furnace tune-up and filter", low_cents=14_000, high_cents=26_000),
        _I(description="Replace condenser fan motor", low_cents=38_000, high_cents=72_000),
        _I(description="Recharge refrigerant", low_cents=25_000, high_cents=48_000),
        _I(description="Replace thermostat", low_cents=16_000, high_cents=34_000),
        _I(description="HVAC labor", low_cents=11_000, high_cents=16_000, unit="hour"),
    ],
    "roofing": [
        _I(description="Repair roof leak and flashing", low_cents=45_000, high_cents=120_000),
        _I(description="Replace damaged shingles", low_cents=35_000, high_cents=90_000),
        _I(description="Seal skylight", low_cents=18_000, high_cents=40_000),
        _I(description="Roofing crew labor", low_cents=12_000, high_cents=18_000, unit="hour"),
    ],
    "painting": [
        _I(description="Interior repaint, two coats", low_cents=90_000, high_cents=240_000),
        _I(description="Patch and paint ceiling stain", low_cents=22_000, high_cents=48_000),
        _I(description="Paint trim and doors", low_cents=30_000, high_cents=75_000),
        _I(description="Paint and supplies", low_cents=8_000, high_cents=22_000, unit="lot"),
    ],
    "landscaping": [
        _I(
            description="Monthly grounds maintenance",
            low_cents=35_000,
            high_cents=80_000,
            unit="visit",
        ),
        _I(description="Hedge trimming", low_cents=15_000, high_cents=38_000),
        _I(description="Mulch installation", low_cents=20_000, high_cents=55_000),
        _I(description="Irrigation repair", low_cents=12_000, high_cents=36_000),
    ],
    "cleaning": [
        _I(description="Move-out deep clean", low_cents=28_000, high_cents=52_000),
        _I(description="Common area cleaning", low_cents=14_000, high_cents=30_000, unit="visit"),
        _I(description="Appliance interior cleaning", low_cents=6_000, high_cents=14_000),
    ],
    "appliance": [
        _I(description="Replace dishwasher pump", low_cents=22_000, high_cents=38_000),
        _I(description="Refrigerator compressor diagnosis", low_cents=9_500, high_cents=16_000),
        _I(description="Replace oven igniter", low_cents=16_000, high_cents=29_000),
        _I(description="Washer drain repair", low_cents=14_000, high_cents=26_000),
    ],
    "locksmith": [
        _I(description="Rekey unit locks", low_cents=9_000, high_cents=18_000),
        _I(description="Replace deadbolt", low_cents=12_000, high_cents=24_000),
        _I(
            description="Emergency lockout service",
            low_cents=11_000,
            high_cents=19_000,
            unit="visit",
        ),
    ],
    "pest": [
        _I(
            description="Quarterly pest treatment",
            low_cents=12_000,
            high_cents=24_000,
            unit="visit",
        ),
        _I(description="Rodent exclusion", low_cents=28_000, high_cents=60_000),
        _I(description="Bed bug heat treatment", low_cents=90_000, high_cents=160_000),
    ],
    "flooring": [
        _I(description="Replace vinyl plank flooring", low_cents=120_000, high_cents=260_000),
        _I(description="Repair subfloor section", low_cents=30_000, high_cents=70_000),
        _I(description="Install transition strips", low_cents=6_000, high_cents=15_000),
    ],
    "handyman": [
        _I(description="Rehang interior door", low_cents=9_000, high_cents=18_000),
        _I(description="Repair cabinet hinges", low_cents=6_500, high_cents=14_000),
        _I(description="Install towel bars and hardware", low_cents=7_000, high_cents=15_000),
        _I(description="Handyman labor", low_cents=7_500, high_cents=11_000, unit="hour"),
    ],
    "glass": [
        _I(description="Replace broken window pane", low_cents=22_000, high_cents=48_000),
        _I(description="Replace sliding door glass", low_cents=60_000, high_cents=120_000),
        _I(description="Reseal window frame", low_cents=9_000, high_cents=20_000),
    ],
    "gutters": [
        _I(description="Clean gutters and downspouts", low_cents=18_000, high_cents=38_000),
        _I(description="Replace gutter section", low_cents=26_000, high_cents=60_000),
        _I(description="Install gutter guards", low_cents=40_000, high_cents=90_000),
    ],
    "snow": [
        _I(
            description="Snow plowing, parking lot",
            low_cents=22_000,
            high_cents=45_000,
            unit="visit",
        ),
        _I(description="Sidewalk de-icing", low_cents=8_000, high_cents=18_000, unit="visit"),
    ],
    "carpet": [
        _I(description="Steam clean carpets", low_cents=16_000, high_cents=34_000),
        _I(description="Stain treatment", low_cents=5_000, high_cents=12_000),
        _I(description="Carpet stretching", low_cents=12_000, high_cents=26_000),
    ],
    "masonry": [
        _I(description="Repoint brick wall", low_cents=60_000, high_cents=140_000),
        _I(description="Repair concrete steps", low_cents=45_000, high_cents=110_000),
        _I(description="Seal retaining wall", low_cents=20_000, high_cents=45_000),
    ],
    "tree": [
        _I(description="Remove hazard limb", low_cents=30_000, high_cents=75_000),
        _I(description="Stump grinding", low_cents=15_000, high_cents=40_000),
        _I(description="Crown thinning", low_cents=40_000, high_cents=95_000),
    ],
    "drywall": [
        _I(description="Patch drywall holes", low_cents=12_000, high_cents=30_000),
        _I(description="Replace water-damaged drywall", low_cents=45_000, high_cents=110_000),
        _I(description="Texture match and prime", low_cents=9_000, high_cents=22_000),
    ],
    "windows": [
        _I(
            description="Exterior window washing", low_cents=24_000, high_cents=52_000, unit="visit"
        ),
        _I(description="Screen replacement", low_cents=4_500, high_cents=9_500),
    ],
    "fencing": [
        _I(description="Replace fence panels", low_cents=35_000, high_cents=85_000),
        _I(description="Repair gate latch and hinges", low_cents=9_000, high_cents=21_000),
        _I(description="Set new fence post", low_cents=14_000, high_cents=30_000),
    ],
    "chimney": [
        _I(description="Chimney sweep and inspection", low_cents=19_000, high_cents=32_000),
        _I(description="Replace chimney cap", low_cents=22_000, high_cents=45_000),
    ],
    "paving": [
        _I(description="Seal-coat parking lot", low_cents=120_000, high_cents=280_000),
        _I(description="Patch potholes", low_cents=30_000, high_cents=75_000),
    ],
    "restoration": [
        _I(description="Water extraction", low_cents=60_000, high_cents=140_000),
        _I(description="Dehumidifier rental (3 days)", low_cents=24_000, high_cents=45_000),
    ],
    "hauling": [
        _I(description="Junk removal, half truck", low_cents=22_000, high_cents=38_000),
        _I(description="Appliance haul-away", low_cents=7_500, high_cents=15_000),
    ],
    "garage": [
        _I(description="Replace garage door spring", low_cents=18_000, high_cents=32_000),
        _I(description="Garage opener repair", low_cents=14_000, high_cents=28_000),
    ],
}

# Large jobs used for over-cap work orders (estimate above the $5,000 R7 cap).
_LARGE_JOBS: dict[str, list[tuple[str, int]]] = {
    "roofing": [
        ("Tear-off and re-roof, building section", 520_000),
        ("Roofing crew labor", 96_000),
    ],
    "hvac": [("Replace rooftop HVAC unit", 610_000), ("Crane rental", 85_000)],
    "flooring": [("Replace flooring, whole unit", 480_000), ("Haul-away and disposal", 42_000)],
    "paving": [("Resurface parking lot", 590_000), ("Line striping", 38_000)],
}


def remit_text(vendor_slug: str, legal_name: str, account: str | None = None) -> str:
    """The remit-to block a vendor prints (test routing number, fictional account)."""
    acct = account or _account_for(vendor_slug)
    return f"Remit to: {legal_name}, {TEST_BANK_NAME}, Routing {TEST_ROUTING_NUMBER}, Acct {acct}"


def _account_for(vendor_slug: str) -> str:
    idx = [v[0] for v in _VENDORS].index(vendor_slug)
    tail = (7919 * (idx + 3) + 1234) % 10_000
    return f"000999{idx:02d}{tail:04d}"


class Universe(_M):
    tenant_id: str = TENANT_ID
    tenant_name: str = TENANT_NAME
    seed: int
    as_of: dt.date
    properties: tuple[PropertyRec, ...]
    units: tuple[UnitRec, ...]
    vendors: tuple[VendorRec, ...]
    work_orders: tuple[WorkOrderRec, ...]
    historical_bills: tuple[HistoricalBillRec, ...]
    staff: tuple[StaffRec, ...]
    catalog: dict[str, list[CatalogItem]] = Field(default_factory=dict)
    large_jobs: dict[str, list[tuple[str, int]]] = Field(default_factory=dict)

    # ------------------------------------------------------------------ lookups
    def vendor_by_slug(self, slug: str) -> VendorRec:
        return next(v for v in self.vendors if v.slug == slug)

    def vendor_by_id(self, vendor_id: str) -> VendorRec:
        return next(v for v in self.vendors if v.id == vendor_id)

    def work_order(self, wo_number: str) -> WorkOrderRec:
        return next(w for w in self.work_orders if w.wo_number == wo_number)

    def unit_by_id(self, unit_id: str) -> UnitRec:
        return next(u for u in self.units if u.id == unit_id)

    def property_of_unit(self, unit_id: str) -> PropertyRec:
        pid = self.unit_by_id(unit_id).property_id
        return next(p for p in self.properties if p.id == pid)

    def unit_by_label(self, label: str) -> UnitRec:
        return next(u for u in self.units if u.label == label)

    def remit_text_for(self, vendor: VendorRec) -> str:
        return remit_text(vendor.slug, vendor.legal_name)

    # ------------------------------------------------------------------ contract view
    def to_tenant_data(self, *, include_history: bool = True) -> TenantData:
        """The same view ``invoicedesk.db.repo.load_tenant_data`` returns after seeding."""
        prior = (
            tuple(
                PriorInvoice(
                    source="history",
                    ref_id=h.id,
                    vendor_id=h.vendor_id,
                    invoice_number=h.invoice_number,
                    total_cents=h.total_cents,
                    invoice_date=h.invoice_date,
                )
                for h in self.historical_bills
            )
            if include_history
            else ()
        )
        return TenantData(
            tenant_id=self.tenant_id,
            properties=tuple(
                Property(id=p.id, name=p.name, address=p.address) for p in self.properties
            ),
            units=tuple(
                Unit(id=u.id, property_id=u.property_id, label=u.label) for u in self.units
            ),
            vendors=tuple(
                Vendor(
                    id=v.id,
                    legal_name=v.legal_name,
                    aliases=v.aliases,
                    address=v.address,
                    tax_id_last4=v.tax_id_last4,
                    remit_to_hash=v.remit_to_hash,
                    bank_last4=v.bank_last4,
                    phone_on_file=v.phone_on_file,
                    email=v.email,
                    status=v.status,
                )
                for v in self.vendors
            ),
            work_orders=tuple(
                WorkOrder(
                    id=w.id,
                    wo_number=w.wo_number,
                    unit_id=w.unit_id,
                    vendor_id=w.vendor_id,
                    scope=w.scope,
                    status=w.status,
                    approved_estimate_cents=w.approved_estimate_cents,
                    estimate_lines=tuple(
                        EstimateLine(description=e.description, amount_cents=e.amount_cents)
                        for e in w.estimate_lines
                    ),
                )
                for w in self.work_orders
            ),
            prior_invoices=prior,
        )


# --------------------------------------------------------------------------- builder
def _round5(cents: int) -> int:
    return max(500, (cents // 500) * 500)


def _estimate_lines(
    rng: random.Random, wo_id: str, trade: str, *, n_lines: int
) -> tuple[EstimateLineRec, ...]:
    items = _CATALOG[trade]
    chosen = rng.sample(items, k=min(n_lines, len(items)))
    out: list[EstimateLineRec] = []
    for i, it in enumerate(chosen):
        qty = rng.randint(2, 6) if it.unit == "hour" else 1
        unit_price = _round5(rng.randint(it.low_cents, it.high_cents))
        out.append(
            EstimateLineRec(
                id=stable_id(wo_id, "line", i),
                description=it.description if it.unit != "hour" else f"{it.description} ({qty} hr)",
                amount_cents=unit_price * qty,
                quantity=qty,
                unit=it.unit,
            )
        )
    return tuple(out)


def build_universe(seed: int = SEED, *, as_of: dt.date = AS_OF) -> Universe:  # noqa: PLR0915
    """Build the full deterministic universe. Same ``seed`` -> identical output."""
    rng = random.Random(seed)  # noqa: S311 - reproducible synthetic data
    fake = Faker("en_US")
    fake.seed_instance(seed)

    # ---- properties and units
    properties: list[PropertyRec] = []
    units: list[UnitRec] = []
    for slug, name, labels in _PROPERTIES:
        pid = stable_id(TENANT_ID, "property", slug)
        street = f"{rng.randint(100, 2999)} {fake.street_name()}"
        addr = f"{street}, Springfield, OR 974{rng.randint(10, 99)}"
        properties.append(PropertyRec(id=pid, slug=slug, name=name, address=addr))
        units.extend(
            UnitRec(id=stable_id(TENANT_ID, "unit", slug, lab), property_id=pid, label=lab)
            for lab in labels
        )

    # ---- vendors
    vendors: list[VendorRec] = []
    used_last4: set[str] = set()
    for idx, (slug, name, aliases, trade, vstatus) in enumerate(_VENDORS):
        vid = stable_id(TENANT_ID, "vendor", slug)
        if slug == "rapid-rooter":
            address = "118 Alder Way, Springfield, OR 97477"
            tax4 = "4821"
        else:
            street = f"{rng.randint(10, 9899)} {fake.street_name()}"
            address = f"{street}, Springfield, OR 974{rng.randint(10, 99)}"
            tax4 = f"{rng.randint(0, 9999):04d}"
            while tax4 in used_last4 or tax4 == "4821":
                tax4 = f"{rng.randint(0, 9999):04d}"
        used_last4.add(tax4)
        account = _account_for(slug)
        remit = remit_text(slug, name, account)
        fp = remit_fingerprint(remit)
        assert fp is not None
        domain = slug.replace("-", "") + ".example"
        vendors.append(
            VendorRec(
                id=vid,
                slug=slug,
                legal_name=name,
                aliases=aliases,
                trade=trade,
                address=address,
                tax_id_last4=tax4,
                remit_to_hash=fp,
                bank_last4=account[-4:],
                phone_on_file=f"(541) 555-01{idx + 10:02d}",
                email=f"billing@{domain}",
                qbo_vendor_ref=str(100 + idx),
                status=vstatus,  # type: ignore[arg-type]
                number_format="INV-{n}" if slug == "rapid-rooter" else _NUMBER_FORMATS[idx % 8],
                number_base=2200 if slug == "rapid-rooter" else 1000 + rng.randint(1, 80) * 100,
                tax_rate_bps=725 if slug == "rapid-rooter" else rng.choice((0, 0, 600, 725, 825)),
                payment_terms_days=rng.choice((15, 30, 30, 45)),
                template="trades" if slug == "rapid-rooter" else TEMPLATES[idx % len(TEMPLATES)],
                accent=_ACCENTS[idx % len(_ACCENTS)],
                prints_remit=slug == "rapid-rooter" or rng.random() < 0.4,
                prints_tax_id=slug == "rapid-rooter" or rng.random() < 0.7,
            )
        )
    by_trade: dict[str, list[VendorRec]] = {}
    for v in vendors:
        if v.status != "blocked":
            by_trade.setdefault(v.trade, []).append(v)
    blocked = [v for v in vendors if v.status == "blocked"]
    rapid = next(v for v in vendors if v.slug == "rapid-rooter")
    unit_by_label = {u.label: u for u in units}

    # ---- work orders WO-1100 .. WO-1259
    approvers = ("Priya Okafor", "Sam Marlowe")
    work_orders: list[WorkOrderRec] = []
    trades = sorted(by_trade)
    large_trades = sorted(t for t in _LARGE_JOBS if t in by_trade)
    for i in range(160):
        number = 1100 + i
        wo_number = f"WO-{number}"
        wo_id = stable_id(TENANT_ID, "wo", wo_number)
        approved_at = as_of - dt.timedelta(days=rng.randint(20, 540))
        if wo_number == "WO-1187":
            lines = (
                EstimateLineRec(
                    id=stable_id(wo_id, "line", 0),
                    description="Replace kitchen drain line",
                    amount_cents=215_000,
                ),
                EstimateLineRec(
                    id=stable_id(wo_id, "line", 1),
                    description="Replace P-trap and supply valves",
                    amount_cents=65_000,
                ),
                EstimateLineRec(
                    id=stable_id(wo_id, "line", 2),
                    description="Plumbing labor (4 hr)",
                    amount_cents=40_000,
                    quantity=4,
                    unit="hour",
                ),
            )
            work_orders.append(
                WorkOrderRec(
                    id=wo_id,
                    wo_number=wo_number,
                    unit_id=unit_by_label["12B"].id,
                    vendor_id=rapid.id,
                    scope="Kitchen drain line replacement, Unit 12B",
                    status="open",
                    approved_estimate_cents=sum(x.amount_cents for x in lines),
                    approved_by=approvers[0],
                    approved_at=as_of - dt.timedelta(days=30),
                    estimate_lines=lines,
                )
            )
            continue
        if wo_number == "WO-1179":
            lines = (
                EstimateLineRec(
                    id=stable_id(wo_id, "line", 0),
                    description="Replace kitchen drain trap",
                    amount_cents=85_000,
                ),
                EstimateLineRec(
                    id=stable_id(wo_id, "line", 1),
                    description="Plumbing labor (2 hr)",
                    amount_cents=30_000,
                    quantity=2,
                    unit="hour",
                ),
            )
            work_orders.append(
                WorkOrderRec(
                    id=wo_id,
                    wo_number=wo_number,
                    unit_id=unit_by_label["10A"].id,
                    vendor_id=rapid.id,
                    scope="Kitchen drain trap replacement, Unit 10A",
                    status="open",
                    approved_estimate_cents=sum(x.amount_cents for x in lines),
                    approved_by=approvers[1],
                    approved_at=as_of - dt.timedelta(days=21),
                    estimate_lines=lines,
                )
            )
            continue
        unit = rng.choice(units)
        large = i % 23 == 7  # 7 large jobs (over the $5,000 cap)
        if large:
            trade = large_trades[(i // 23) % len(large_trades)]
            vendor = rng.choice(by_trade[trade])
            items = _LARGE_JOBS[trade]
            lines = tuple(
                EstimateLineRec(id=stable_id(wo_id, "line", j), description=d, amount_cents=c)
                for j, (d, c) in enumerate(items)
            )
            scope = f"{items[0][0]}, {_unit_text(unit, properties)}"
        else:
            if i % 37 == 11:
                vendor = blocked[(i // 37) % len(blocked)]
                trade = vendor.trade
            else:
                trade = trades[rng.randrange(len(trades))]
                vendor = rng.choice(by_trade[trade])
            lines = _estimate_lines(rng, wo_id, trade, n_lines=rng.randint(1, 3))
            scope = f"{lines[0].description}, {_unit_text(unit, properties)}"
        status: Literal["open", "closed"] = (
            "closed" if (rng.random() < 0.3 or vendor.status == "blocked") else "open"
        )
        work_orders.append(
            WorkOrderRec(
                id=wo_id,
                wo_number=wo_number,
                unit_id=unit.id,
                vendor_id=vendor.id,
                scope=scope,
                status=status,
                approved_estimate_cents=sum(x.amount_cents for x in lines),
                approved_by=rng.choice(approvers),
                approved_at=approved_at,
                estimate_lines=lines,
            )
        )

    # ---- 18 months of history: 2025-01-10 .. 2026-07-10, plus INV-2231 on 2026-10-10
    hist_start = as_of - dt.timedelta(days=648)
    hist_end = as_of - dt.timedelta(days=102)
    span = (hist_end - hist_start).days
    history: list[HistoricalBillRec] = []
    wos_by_vendor: dict[str, list[WorkOrderRec]] = {}
    for w in work_orders:
        wos_by_vendor.setdefault(w.vendor_id, []).append(w)
    for v in vendors:
        if v.status == "pending":
            continue  # pending vendors have never been paid
        n = 4 if v.slug == "rapid-rooter" else rng.randint(3, 9)
        dates = sorted(hist_start + dt.timedelta(days=rng.randrange(span)) for _ in range(n))
        start = v.number_base + (31 - n if v.slug == "rapid-rooter" else 0)
        for k, d in enumerate(dates):
            num = start + k
            wo = rng.choice(wos_by_vendor[v.id]) if wos_by_vendor.get(v.id) else None
            base = wo.approved_estimate_cents if wo else rng.randint(15_000, 180_000)
            pre = min(base, 400_000)
            total = pre + (pre * v.tax_rate_bps) // 10_000
            inv = v.number_format.format(n=num)
            history.append(
                HistoricalBillRec(
                    id=stable_id(TENANT_ID, "hist", v.slug, inv),
                    vendor_id=v.id,
                    invoice_number=inv,
                    invoice_date=d,
                    total_cents=total,
                    work_order_id=wo.id if wo else None,
                )
            )
    wo1179 = next(w for w in work_orders if w.wo_number == "WO-1179")
    history.append(
        HistoricalBillRec(
            id=stable_id(TENANT_ID, "hist", "rapid-rooter", "INV-2231"),
            vendor_id=rapid.id,
            invoice_number="INV-2231",
            invoice_date=dt.date(2026, 10, 10),
            total_cents=124_000,
            work_order_id=wo1179.id,
        )
    )

    staff = (
        StaffRec(email="dana.clerk@maplerow.example", display_name="Dana Whitlock", role="clerk"),
        StaffRec(
            email="priya.approver@maplerow.example", display_name="Priya Okafor", role="approver"
        ),
        StaffRec(email="sam.owner@maplerow.example", display_name="Sam Marlowe", role="owner"),
    )
    return Universe(
        seed=seed,
        as_of=as_of,
        properties=tuple(properties),
        units=tuple(units),
        vendors=tuple(vendors),
        work_orders=tuple(work_orders),
        historical_bills=tuple(history),
        staff=staff,
        catalog=_CATALOG,
        large_jobs=_LARGE_JOBS,
    )


def _unit_text(unit: UnitRec, properties: list[PropertyRec]) -> str:
    prop = next(p for p in properties if p.id == unit.property_id)
    return f"Unit {unit.label}, {prop.name}"
