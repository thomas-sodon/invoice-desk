"""Calibrators: a monotone heuristic (no artifact) and fitted combiners (JSON artifacts).

Fitted calibrators are trained with scikit-learn on the DEV split only, but inference is plain
numpy over the saved coefficients/thresholds, so a saved artifact is portable and deterministic.

Methods:
* ``logistic``: logistic regression over :data:`FEATURE_NAMES` (Platt-style combiner).
* ``logistic_isotonic``: the logistic score, then an isotonic map fitted on top.
* ``isotonic``: an isotonic map over the heuristic score (monotone, few parameters).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import numpy as np

from invoicedesk.calibrate.features import FEATURE_NAMES, feature_vector
from invoicedesk.contracts.calibration import FieldSignals

__all__ = [
    "FittedCalibrator",
    "HeuristicCalibrator",
    "Method",
    "assert_dev_split",
    "fit_calibrator",
]

Method = Literal["logistic", "logistic_isotonic", "isotonic"]
ARTIFACT_KIND = "invoicedesk.calibrator"


def assert_dev_split(split: str) -> None:
    """Calibrators and thresholds are tuned on the dev split only (CLAUDE.md Evals)."""
    if split != "dev":
        raise ValueError(f"refusing to fit on split {split!r}: tune on 'dev' only")


class HeuristicCalibrator:
    """Monotone hand-set combiner used until a fitted artifact exists.

    ``p = raw * (0.5 + 0.5 * ocr_agreement)``, then: arithmetic failure x0.5, cross-tier
    disagreement x0.6, cross-tier agreement halves the remaining error, no cited OCR words
    x0.85, handwriting x0.9. Non-decreasing in ``raw_conf`` and ``ocr_agreement``; never
    raises a value above its raw self-report except through cross-tier agreement.
    """

    version = "heuristic-v1"

    def score(self, s: FieldSignals) -> float:
        p = s.raw_conf * (0.5 + 0.5 * s.ocr_agreement)
        if s.arithmetic_ok is False:
            p *= 0.5
        if s.cross_tier_agree is False:
            p *= 0.6
        elif s.cross_tier_agree is True:
            p = 1.0 - (1.0 - p) * 0.5
        if not s.has_source_words:
            p *= 0.85
        if s.handwriting:
            p *= 0.9
        return min(1.0, max(0.0, p))

    def calibrate(self, signals: list[FieldSignals]) -> dict[str, float]:
        return {s.field: self.score(s) for s in signals}


_HEURISTIC = HeuristicCalibrator()


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


class FittedCalibrator:
    def __init__(
        self,
        *,
        method: Method,
        coef: Sequence[float] | None = None,
        intercept: float = 0.0,
        iso_x: Sequence[float] | None = None,
        iso_y: Sequence[float] | None = None,
        n_train: int = 0,
        version: str | None = None,
        feature_names: Sequence[str] = FEATURE_NAMES,
    ) -> None:
        if tuple(feature_names) != FEATURE_NAMES:
            raise ValueError("artifact feature names do not match this code version")
        if method in ("logistic", "logistic_isotonic") and coef is None:
            raise ValueError(f"{method} needs coefficients")
        if method in ("logistic_isotonic", "isotonic") and (iso_x is None or iso_y is None):
            raise ValueError(f"{method} needs isotonic thresholds")
        self.method: Method = method
        self.coef = None if coef is None else [float(c) for c in coef]
        self.intercept = float(intercept)
        self.iso_x = None if iso_x is None else [float(v) for v in iso_x]
        self.iso_y = None if iso_y is None else [float(v) for v in iso_y]
        self.n_train = n_train
        self.version = version or f"{method}-{self._digest()}"

    # ------------------------------------------------------------------ inference
    def _base_scores(self, signals: Sequence[FieldSignals]) -> np.ndarray:
        if self.method == "isotonic":
            return np.array([_HEURISTIC.score(s) for s in signals], dtype=float)
        x = np.array([feature_vector(s) for s in signals], dtype=float)
        assert self.coef is not None
        return _sigmoid(x @ np.array(self.coef) + self.intercept)

    def predict(self, signals: Sequence[FieldSignals]) -> np.ndarray:
        if not signals:
            return np.zeros(0)
        p = self._base_scores(signals)
        if self.method in ("logistic_isotonic", "isotonic"):
            assert self.iso_x is not None
            assert self.iso_y is not None
            p = np.interp(p, self.iso_x, self.iso_y)
        return np.clip(p, 0.0, 1.0)

    def calibrate(self, signals: list[FieldSignals]) -> dict[str, float]:
        probs = self.predict(signals)
        return {s.field: float(p) for s, p in zip(signals, probs, strict=True)}

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": ARTIFACT_KIND,
            "version": self.version,
            "method": self.method,
            "feature_names": list(FEATURE_NAMES),
            "coef": self.coef,
            "intercept": self.intercept,
            "iso_x": self.iso_x,
            "iso_y": self.iso_y,
            "n_train": self.n_train,
        }

    def _digest(self) -> str:
        body = json.dumps(
            [self.method, self.coef, self.intercept, self.iso_x, self.iso_y], sort_keys=True
        )
        return hashlib.sha256(body.encode()).hexdigest()[:10]

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FittedCalibrator:
        if d.get("kind") != ARTIFACT_KIND:
            raise ValueError("not an invoicedesk calibrator artifact")
        return cls(
            method=d["method"],
            coef=d.get("coef"),
            intercept=d.get("intercept", 0.0),
            iso_x=d.get("iso_x"),
            iso_y=d.get("iso_y"),
            n_train=int(d.get("n_train", 0)),
            version=d.get("version"),
            feature_names=d.get("feature_names", FEATURE_NAMES),
        )

    @classmethod
    def load(cls, path: Path) -> FittedCalibrator:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def fit_calibrator(
    signals: Sequence[FieldSignals],
    labels: Sequence[bool],
    *,
    split: str,
    method: Method = "logistic",
    c: float = 1.0,
) -> FittedCalibrator:
    """Fit on labeled dev fields (``labels[i]``: was field ``i`` exactly right?)."""
    assert_dev_split(split)
    if len(signals) != len(labels):
        raise ValueError("signals and labels differ in length")
    if len(signals) < 10:
        raise ValueError("need at least 10 labeled fields to fit a calibrator")
    y = np.array([1 if v else 0 for v in labels], dtype=int)
    if y.min() == y.max():
        raise ValueError("labels are all one class; cannot fit")

    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression

    coef: list[float] | None = None
    intercept = 0.0
    if method in ("logistic", "logistic_isotonic"):
        x = np.array([feature_vector(s) for s in signals], dtype=float)
        lr = LogisticRegression(C=c, max_iter=1000)
        lr.fit(x, y)
        coef = [float(v) for v in lr.coef_[0]]
        intercept = float(np.asarray(lr.intercept_).ravel()[0])
    if method == "logistic":
        return FittedCalibrator(method=method, coef=coef, intercept=intercept, n_train=len(y))

    if method == "logistic_isotonic":
        assert coef is not None
        x = np.array([feature_vector(s) for s in signals], dtype=float)
        base = _sigmoid(x @ np.array(coef) + intercept)
    else:
        base = np.array([_HEURISTIC.score(s) for s in signals], dtype=float)
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(base, y)
    iso_x = [float(v) for v in iso.X_thresholds_]
    iso_y = [float(v) for v in iso.y_thresholds_]
    if any(math.isnan(v) for v in iso_y):  # pragma: no cover - sklearn guarantees finite
        raise ValueError("isotonic fit produced NaN")
    return FittedCalibrator(
        method=method,
        coef=coef,
        intercept=intercept,
        iso_x=iso_x,
        iso_y=iso_y,
        n_train=len(y),
    )
