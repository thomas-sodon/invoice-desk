"""Confidence calibration (M6).

A learned combiner maps per-field signals (model self-report, OCR agreement, arithmetic
consistency, cross-tier agreement, handwriting) to the probability that the field is exactly
right. It is fitted on the DEV split only; thresholds ``tau_field``/``tau_hi`` are chosen on the
same split to keep false-STP at or below 1%. ECE (10 bins) and a reliability diagram are
published by the evals component from :mod:`invoicedesk.calibrate.metrics`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from invoicedesk.calibrate.combiner import (
    FittedCalibrator,
    HeuristicCalibrator,
    assert_dev_split,
    fit_calibrator,
)
from invoicedesk.calibrate.features import (
    FEATURE_NAMES,
    build_field_signals,
    feature_vector,
    iter_scored_fields,
    ocr_agreement,
)
from invoicedesk.calibrate.metrics import (
    ReliabilityBin,
    brier_score,
    ece,
    plot_reliability,
    reliability_bins,
)
from invoicedesk.calibrate.thresholds import DocScores, ThresholdChoice, choose_thresholds
from invoicedesk.contracts.calibration import Calibrator

__all__ = [
    "CALIBRATOR_ENV",
    "FEATURE_NAMES",
    "DocScores",
    "FittedCalibrator",
    "HeuristicCalibrator",
    "ReliabilityBin",
    "ThresholdChoice",
    "assert_dev_split",
    "brier_score",
    "build_field_signals",
    "choose_thresholds",
    "ece",
    "feature_vector",
    "fit_calibrator",
    "iter_scored_fields",
    "load_calibrator",
    "ocr_agreement",
    "plot_reliability",
    "reliability_bins",
]

log = logging.getLogger(__name__)

#: Optional env var naming a fitted calibrator artifact (JSON) to load by default.
CALIBRATOR_ENV = "INVOICEDESK_CALIBRATOR"


def load_calibrator(path: Path | None = None) -> Calibrator:
    """Load a fitted calibrator artifact.

    ``path=None`` reads ``$INVOICEDESK_CALIBRATOR`` if set. When no artifact exists, returns the
    monotone :class:`HeuristicCalibrator` (version ``heuristic-v1``), so the pipeline always has
    a calibrated-probability source and R9 never falls back to raw self-report.
    """
    if path is None:
        env = os.environ.get(CALIBRATOR_ENV)
        path = Path(env) if env else None
    if path is None:
        return HeuristicCalibrator()
    if not Path(path).exists():
        log.warning("calibrator artifact %s not found; using heuristic-v1", path)
        return HeuristicCalibrator()
    return FittedCalibrator.load(Path(path))
