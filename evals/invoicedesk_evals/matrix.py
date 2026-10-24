"""The experiment matrix (``evals/configs/matrix.yaml``, DESIGN §5.6)."""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CONFIG_ALIASES",
    "MATRIX_PATH",
    "ConfigSpec",
    "Matrix",
    "load_matrix",
    "resolve_config_name",
]

EVALS_DIR = Path(__file__).resolve().parents[1]
MATRIX_PATH = EVALS_DIR / "configs" / "matrix.yaml"

#: Shell-friendly spellings of D' (a quote in ``make eval CONFIG=D'`` is awkward).
CONFIG_ALIASES = {"DP": "D'", "D-PRIME": "D'", "DPRIME": "D'", "D_PRIME": "D'"}

Tier = Literal["haiku", "sonnet", "opus"]


class ConfigSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    description: str
    pipeline: Literal["invoicedesk", "naive"] = "invoicedesk"
    extractor: Literal["claude", "azure_di"] = "claude"
    tiers: list[Tier] = Field(default_factory=list)
    caching: bool = True
    images: bool = True
    media: list[str] | None = None
    requires_env: list[str] = Field(default_factory=list)
    est_usd_per_doc: Decimal

    @property
    def routed(self) -> bool:
        return len(self.tiers) > 1

    @property
    def max_tier(self) -> Tier:
        """``max_tier`` passed to the pipeline: routed configs may reach Opus; single-tier
        configs stop after the first call (whose role is remapped to the chosen model)."""
        return "opus" if self.routed else "haiku"

    def missing_env(self) -> list[str]:
        return [k for k in self.requires_env if not os.environ.get(k)]


class Matrix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    default_repeats: int = 3
    default_split: str = "test"
    configs: dict[str, ConfigSpec]

    def get(self, name: str) -> ConfigSpec:
        key = resolve_config_name(name)
        if key not in self.configs:
            raise KeyError(f"unknown config {name!r}; known: {', '.join(self.configs)}")
        return self.configs[key]


def resolve_config_name(name: str) -> str:
    n = name.strip()
    return CONFIG_ALIASES.get(n.upper(), n.upper() if len(n) <= 2 else n)


@lru_cache(maxsize=4)
def load_matrix(path: Path = MATRIX_PATH) -> Matrix:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    m = Matrix.model_validate(raw)
    for k, spec in m.configs.items():
        spec.name = k
    return m
