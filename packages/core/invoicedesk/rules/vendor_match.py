"""Vendor resolution against the vendor master (shared by R1, R2, R3, R4 and R6).

Order of checks (first hit wins):

1. ``exact``: the normalized name equals a vendor's legal name or alias.
2. ``homoglyph``: the confusable skeleton equals a vendor's, but the name does not (``Rаpid``
   with a Cyrillic ``а``, ``Rapid R00ter``).
3. ``mixed_script``: the name mixes Latin with Cyrillic or Greek letters.
4. ``edit_distance``: within ``lookalike_max_edit_distance`` of a vendor name (``Rapid Rotor``
   vs ``Rapid Rooter``).
5. ``fuzzy``: rapidfuzz token-set score >= ``fuzzy_match_min_score`` AND corroborated by the
   tax-ID last 4 or the address. Without corroboration the vendor stays unknown.
6. ``unknown``.

Only ``exact`` and ``fuzzy`` resolve a ``vendor_id``. A lookalike never resolves to the real
vendor, so its invoice can never borrow that vendor's work orders or remit-to record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from invoicedesk.config.tenant import RulesConfig
from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.tenant_data import TenantData, Vendor
from invoicedesk.rules._text import basic_norm, is_mixed_script, skeleton

__all__ = ["VendorMatch", "resolve_vendor"]

Method = Literal["exact", "fuzzy", "homoglyph", "mixed_script", "edit_distance", "unknown"]


@dataclass(frozen=True, slots=True)
class VendorMatch:
    method: Method
    vendor: Vendor | None = None  # resolved vendor (exact / fuzzy only)
    resembles: Vendor | None = None  # nearest real vendor for lookalikes and near-misses
    score: float = 0.0
    distance: int | None = None
    evidence: dict[str, bool] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.vendor is not None

    @property
    def lookalike(self) -> bool:
        return self.method in ("homoglyph", "mixed_script", "edit_distance")


def _names(v: Vendor) -> list[str]:
    return [v.legal_name, *v.aliases]


def _tax_matches(ex: InvoiceExtraction, v: Vendor) -> bool:
    got = ex.vendor_tax_id_last4.value
    return bool(got and v.tax_id_last4 and got.strip()[-4:] == v.tax_id_last4)


def _address_matches(ex: InvoiceExtraction, v: Vendor, min_score: int) -> bool:
    got = ex.vendor_address.value
    if not got or not v.address:
        return False
    return fuzz.token_set_ratio(basic_norm(got), basic_norm(v.address)) >= min_score


def resolve_vendor(ex: InvoiceExtraction, tenant: TenantData, config: RulesConfig) -> VendorMatch:
    raw = (ex.vendor_name.value or "").strip()
    norm = basic_norm(raw)
    if not norm:
        return VendorMatch("unknown")
    vcfg = config.vendor

    for v in tenant.vendors:
        if any(basic_norm(n) == norm for n in _names(v)):
            return VendorMatch("exact", vendor=v, score=100.0, distance=0)

    skel = skeleton(raw)
    if vcfg.check_homoglyphs:
        for v in tenant.vendors:
            if any(skeleton(n) == skel for n in _names(v)):
                return VendorMatch("homoglyph", resembles=v, distance=0)

    # Nearest vendor by edit distance on the skeleton, ties broken by token-set score.
    best: tuple[int, float, Vendor] | None = None
    for v in tenant.vendors:
        for n in _names(v):
            d = Levenshtein.distance(skel, skeleton(n))
            s = fuzz.token_set_ratio(norm, basic_norm(n))
            if best is None or (d, -s) < (best[0], -best[1]):
                best = (d, s, v)

    if vcfg.check_homoglyphs and is_mixed_script(raw):
        return VendorMatch(
            "mixed_script",
            resembles=best[2] if best else None,
            distance=best[0] if best else None,
        )
    if best is None:
        return VendorMatch("unknown")
    dist, _, nearest = best
    if dist <= vcfg.lookalike_max_edit_distance:
        return VendorMatch("edit_distance", resembles=nearest, distance=dist)

    scored = max(
        (
            (max(fuzz.token_set_ratio(norm, basic_norm(n)) for n in _names(v)), v)
            for v in tenant.vendors
        ),
        key=lambda t: t[0],
    )
    score, cand = scored
    if score >= vcfg.fuzzy_match_min_score:
        evidence = {
            "tax_id_last4": _tax_matches(ex, cand),
            "address": _address_matches(ex, cand, vcfg.fuzzy_match_min_score),
        }
        if any(evidence.values()):
            return VendorMatch("fuzzy", vendor=cand, score=score, evidence=evidence)
        return VendorMatch("unknown", resembles=cand, score=score, evidence=evidence)
    return VendorMatch("unknown", score=score)
