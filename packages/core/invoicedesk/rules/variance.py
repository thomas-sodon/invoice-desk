"""Deterministic variance sentence (DESIGN §5.4).

The template is the source of truth: Haiku may later smooth the wording
(``invoicedesk.extract.wording.polish_variance``), but a post-check rejects any rewrite whose
numbers differ from :func:`variance_sentence`.
"""

from __future__ import annotations

from invoicedesk.money import ensure_cents, format_cents

__all__ = ["variance_headline", "variance_sentence"]


def _direction(billed_cents: int, estimate_cents: int) -> tuple[str, int]:
    ensure_cents(billed_cents)
    ensure_cents(estimate_cents)
    diff = billed_cents - estimate_cents
    return ("over" if diff >= 0 else "under"), abs(diff)


def variance_sentence(
    *, vendor: str, billed_cents: int, estimate_cents: int, unit_label: str, wo_number: str
) -> str:
    """``"Rapid Rooter Plumbing invoice is $340.00 over the approved estimate ($900.00) for
    Unit 12B, WO-1187."``"""
    word, amount = _direction(billed_cents, estimate_cents)
    return (
        f"{vendor} invoice is {format_cents(amount)} {word} the approved estimate "
        f"({format_cents(estimate_cents)}) for Unit {unit_label}, {wo_number}."
    )


def variance_headline(
    *, billed_cents: int, estimate_cents: int, unit_label: str, wo_number: str
) -> str:
    """Board-card headline: ``"$340.00 over approved estimate for Unit 12B (WO-1187)"``."""
    word, amount = _direction(billed_cents, estimate_cents)
    return f"{format_cents(amount)} {word} approved estimate for Unit {unit_label} ({wo_number})"
