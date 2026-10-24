from decimal import Decimal
from pathlib import Path

import pytest
from invoicedesk.config.models import (
    HAIKU,
    OPUS,
    ROLE_SETTINGS,
    SONNET,
    TIER_ROLE,
    ModelRole,
    cost_usd,
    settings_for,
)
from invoicedesk.config.tenant import load_rules_config, parse_rules_yaml
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[3]


def test_role_table_matches_claude_md() -> None:
    t1 = settings_for(ModelRole.EXTRACT_T1)
    assert (t1.model, t1.effort, t1.thinking) == (HAIKU, "low", {"type": "disabled"})
    t2 = settings_for(ModelRole.EXTRACT_T2)
    assert (t2.model, t2.effort, t2.thinking) == (SONNET, "low", {"type": "adaptive"})
    t3 = settings_for(ModelRole.ADJUDICATE_T3)
    assert (t3.model, t3.effort, t3.thinking) == (OPUS, "medium", None)
    rc = settings_for(ModelRole.ROOT_CAUSE)
    assert (rc.model, rc.effort, rc.batch) == (OPUS, "high", True)
    assert settings_for(ModelRole.INJECTION_CLASSIFIER).model == HAIKU
    assert TIER_ROLE["sonnet"] is ModelRole.EXTRACT_T2


def test_request_kwargs_never_have_sampling_params() -> None:
    for role, rs in ROLE_SETTINGS.items():
        kw = rs.request_kwargs()
        assert not {"temperature", "top_p", "top_k", "tool_choice", "tools"} & kw.keys(), role
        assert kw["output_config"]["effort"] in ("low", "medium", "high")
        if rs.model == HAIKU:
            assert "fallbacks" not in kw and not rs.uses_beta_endpoint
        else:
            assert kw["fallbacks"] == "default"
            assert kw["betas"] == ["server-side-fallback-2026-07-01"]
            assert rs.uses_beta_endpoint
        if rs.model == OPUS:
            assert "thinking" not in kw


def test_cost() -> None:
    # Haiku extraction §7.2: 3,900 uncached + 3,000 cached + 900 out ~= $0.0009
    c = cost_usd(HAIKU, input_tokens=3900, output_tokens=900, cache_read_input_tokens=3000)
    assert c == Decimal("0.00087000")
    assert cost_usd(SONNET, input_tokens=1_000_000, output_tokens=0) == Decimal("2")
    assert cost_usd(OPUS, input_tokens=0, output_tokens=1_000_000, batch=True) == Decimal("10")
    assert cost_usd(
        HAIKU, input_tokens=0, output_tokens=0, cache_creation_input_tokens=1_000_000
    ) == Decimal("0.125")


def test_tenant_yaml_loads() -> None:
    cfg = load_rules_config("maple_row", REPO / "tenants")
    assert cfg.amount_cap_cents == 500_000
    assert cfg.three_way.tolerance_bps == 1000
    assert cfg.three_way.tolerance_min_cents == 10_000
    assert cfg.duplicate.phash_max_distance == 6
    assert cfg.confidence.threshold("total") == cfg.confidence.tau_field
    assert "total" in cfg.confidence.required_fields


def test_tenant_yaml_rejects_unknown_and_floats_for_money() -> None:
    with pytest.raises(ValidationError):
        parse_rules_yaml("tenant: x\nbogus: 1\n")
    with pytest.raises(ValidationError):
        parse_rules_yaml("tenant: x\namount_cap_cents: 5000.5\n")
    cfg = parse_rules_yaml("tenant: x\nconfidence:\n  per_field: {total: 0.97}\n")
    assert cfg.confidence.threshold("total") == 0.97
