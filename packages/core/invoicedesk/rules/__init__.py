"""Deterministic rules engine R1-R10 (M7; DESIGN §5.5; CLAUDE.md invariants 1, 3, 6, 7).

Each rule is a pure function of :class:`invoicedesk.contracts.rules.RuleContext` living in its
own module (``r01_vendor`` .. ``r10_injection``); config comes from
``tenants/<tenant>/rules.yaml``. Routing is :func:`invoicedesk.status.route`.
"""

from __future__ import annotations

from invoicedesk.contracts.rules import RulesEngine

__all__ = ["build_rules_engine"]


def build_rules_engine() -> RulesEngine:
    """The production engine: all ten rules, in order."""
    from invoicedesk.rules.engine import DeterministicRulesEngine

    return DeterministicRulesEngine()
