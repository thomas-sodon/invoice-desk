"""Single source of truth for Claude model IDs, per-role request settings and prices.

CLAUDE.md: "Model IDs live in one config module; never hardcode them at call sites." Every LLM
call names a :class:`ModelRole`; the client resolves it here. Prices are USD per million tokens
as ``Decimal`` (API cost is not invoice money, but it is still never a float).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

__all__ = [
    "HAIKU",
    "OPUS",
    "PRICES",
    "ROLE_SETTINGS",
    "SERVER_FALLBACK_BETA",
    "SONNET",
    "TIER_ROLE",
    "ModelPrice",
    "ModelRole",
    "RoleSettings",
    "Tier",
    "cost_usd",
    "settings_for",
]

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"

Tier = Literal["haiku", "sonnet", "opus"]
Effort = Literal["low", "medium", "high"]


class ModelRole(StrEnum):
    INJECTION_CLASSIFIER = "injection_classifier"
    EXTRACT_T1 = "extract_t1"
    EXTRACT_T2 = "extract_t2"
    ADJUDICATE_T3 = "adjudicate_t3"
    WORDING = "wording"  # variance sentence polish + vendor follow-up draft
    ROOT_CAUSE = "root_cause"  # offline eval labeling via Batch API


@dataclass(frozen=True, slots=True)
class RoleSettings:
    model: str
    tier: Tier
    effort: Effort
    #: ``None`` means omit the ``thinking`` param (Opus 5.5 cannot disable thinking).
    thinking: dict[str, Any] | None
    max_tokens: int
    #: Server-side fallback (Sonnet/Opus only). Haiku 5.5 has none -> refusal escalates.
    server_fallback: bool = False
    betas: tuple[str, ...] = field(default=())
    batch: bool = False

    @property
    def uses_beta_endpoint(self) -> bool:
        """``fallbacks``/``betas`` exist only on ``client.beta.messages`` in anthropic 2.x."""
        return bool(self.betas) or self.server_fallback

    def request_kwargs(self) -> dict[str, Any]:
        """Keyword args shared by every request for this role (no sampling params, ever)."""
        kw: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "output_config": {"effort": self.effort},
        }
        if self.thinking is not None:
            kw["thinking"] = dict(self.thinking)
        if self.server_fallback:
            kw["fallbacks"] = "default"
        if self.betas:
            kw["betas"] = list(self.betas)
        return kw


ROLE_SETTINGS: dict[ModelRole, RoleSettings] = {
    ModelRole.INJECTION_CLASSIFIER: RoleSettings(
        HAIKU, "haiku", "low", {"type": "disabled"}, max_tokens=1024
    ),
    ModelRole.EXTRACT_T1: RoleSettings(HAIKU, "haiku", "low", {"type": "disabled"}, 4096),
    ModelRole.EXTRACT_T2: RoleSettings(
        SONNET,
        "sonnet",
        "low",
        {"type": "adaptive"},
        8192,
        server_fallback=True,
        betas=(SERVER_FALLBACK_BETA,),
    ),
    ModelRole.ADJUDICATE_T3: RoleSettings(
        OPUS, "opus", "medium", None, 12288, server_fallback=True, betas=(SERVER_FALLBACK_BETA,)
    ),
    ModelRole.WORDING: RoleSettings(HAIKU, "haiku", "low", {"type": "disabled"}, 1024),
    ModelRole.ROOT_CAUSE: RoleSettings(
        OPUS,
        "opus",
        "high",
        None,
        8192,
        server_fallback=True,
        betas=(SERVER_FALLBACK_BETA,),
        batch=True,
    ),
}

TIER_ROLE: dict[Tier, ModelRole] = {
    "haiku": ModelRole.EXTRACT_T1,
    "sonnet": ModelRole.EXTRACT_T2,
    "opus": ModelRole.ADJUDICATE_T3,
}


def settings_for(role: ModelRole) -> RoleSettings:
    return ROLE_SETTINGS[role]


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens (DESIGN §4.4 / §7.2, Oct 2026)."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal  # 5-minute cache write = 1.25x input


def _p(inp: str, out: str, read: str) -> ModelPrice:
    i = Decimal(inp)
    return ModelPrice(i, Decimal(out), Decimal(read), (i * Decimal("1.25")).normalize())


PRICES: dict[str, ModelPrice] = {
    HAIKU: _p("0.10", "0.50", "0.01"),
    SONNET: _p("2", "10", "0.10"),
    OPUS: _p("4", "20", "0.20"),
}

_MTOK = Decimal(1_000_000)


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    batch: bool = False,
) -> Decimal:
    """Cost of one call from ``usage`` x price table. ``input_tokens`` is the uncached input
    (as reported by the API). Batch API halves the price. Quantized to 1e-8 USD."""
    p = PRICES[model]
    c = (
        Decimal(input_tokens) * p.input
        + Decimal(output_tokens) * p.output
        + Decimal(cache_read_input_tokens) * p.cache_read
        + Decimal(cache_creation_input_tokens) * p.cache_write
    ) / _MTOK
    if batch:
        c = c / 2
    return c.quantize(Decimal("0.00000001"))
