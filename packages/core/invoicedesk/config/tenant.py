"""Tenant rules config: ``tenants/<tenant>/rules.yaml`` (CLAUDE.md invariant 7).

Rules are pure functions of (extraction, tenant data, :class:`RulesConfig`). Amounts are integer
cents and percentages are basis points so rule math never touches floats. Confidence thresholds
(``tau_*``) are probabilities and are floats.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from invoicedesk.contracts.extraction import REQUIRED_FIELDS

__all__ = [
    "ArithmeticConfig",
    "ConfidenceConfig",
    "DateConfig",
    "DuplicateConfig",
    "RulesConfig",
    "ThreeWayConfig",
    "VendorConfig",
    "load_rules_config",
]


class _C(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VendorConfig(_C):
    fuzzy_match_min_score: int = Field(
        default=90, ge=0, le=100, description="rapidfuzz score for R1"
    )
    lookalike_max_edit_distance: int = 2
    check_homoglyphs: bool = True


class ThreeWayConfig(_C):
    tolerance_bps: int = 1000  # 10 %
    tolerance_min_cents: int = 10_000  # $100


class DuplicateConfig(_C):
    fuzzy_amount_bps: int = 100  # +/- 1 %
    fuzzy_window_days: int = 7
    phash_max_distance: int = 6


class ArithmeticConfig(_C):
    rounding_slack_cents: int = 1
    tax_rate_min_bps: int = 0
    tax_rate_max_bps: int = 1200


class DateConfig(_C):
    max_age_days: int = 90
    allow_future_days: int = 0


class ConfidenceConfig(_C):
    required_fields: tuple[str, ...] = REQUIRED_FIELDS
    tau_field: float = Field(default=0.90, ge=0, le=1)
    tau_hi: float = Field(default=0.95, ge=0, le=1)
    per_field: dict[str, float] = Field(default_factory=dict)

    def threshold(self, field_name: str) -> float:
        return self.per_field.get(field_name, self.tau_field)


class RulesConfig(_C):
    tenant: str
    amount_cap_cents: int = 500_000  # R7: $5,000
    hours_saved_per_invoice_min: int = 8
    manual_cost_per_invoice_cents: int = 990
    vendor: VendorConfig = VendorConfig()
    three_way: ThreeWayConfig = ThreeWayConfig()
    duplicate: DuplicateConfig = DuplicateConfig()
    arithmetic: ArithmeticConfig = ArithmeticConfig()
    dates: DateConfig = DateConfig()
    confidence: ConfidenceConfig = ConfidenceConfig()


def load_rules_config(tenant: str, tenants_dir: Path | None = None) -> RulesConfig:
    """Load and validate ``<tenants_dir>/<tenant>/rules.yaml`` (unknown keys are an error)."""
    if tenants_dir is None:
        from invoicedesk.config.settings import get_settings

        tenants_dir = get_settings().tenants_dir
    path = Path(tenants_dir) / tenant / "rules.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.setdefault("tenant", tenant)
    return RulesConfig.model_validate(raw)


def parse_rules_yaml(text: str) -> RulesConfig:
    """Validate rules YAML text (used by the DB copy in ``tenants.rules_yaml``)."""
    return RulesConfig.model_validate(yaml.safe_load(text) or {})
