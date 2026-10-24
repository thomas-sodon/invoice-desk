"""Confidence calibration (M6). STUB: owned by the `extraction` component."""

from __future__ import annotations

from pathlib import Path

from invoicedesk.contracts.calibration import Calibrator


def load_calibrator(path: Path | None = None) -> Calibrator:
    """Load a fitted calibrator artifact, or an identity/heuristic one when ``path`` is None."""
    raise NotImplementedError("calibration not built yet")
