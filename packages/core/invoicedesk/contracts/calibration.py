"""Calibrator contract (M6). Maps raw per-field signals to a calibrated probability that the
field value is exactly right. Fitted on the DEV split only."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Calibrator", "FieldSignals"]


class FieldSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    raw_conf: float = Field(ge=0, le=1)
    ocr_agreement: float = Field(ge=0, le=1, description="fuzzy match of value vs cited OCR words")
    arithmetic_ok: bool | None = None  # None when not applicable to the field
    cross_tier_agree: bool | None = None  # None when only one tier ran
    has_source_words: bool = True
    handwriting: bool = False


@runtime_checkable
class Calibrator(Protocol):
    version: str

    def calibrate(self, signals: list[FieldSignals]) -> dict[str, float]:
        """Return ``{field: calibrated_probability}`` for each input signal."""
        ...
