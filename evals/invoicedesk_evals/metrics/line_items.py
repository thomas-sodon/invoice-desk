"""Line-item F1 with Hungarian alignment (DESIGN §5.6).

A predicted line matches a gold line when the descriptions are fuzzy-similar (normalized
``rapidfuzz`` ratio >= 0.8) **and** the amounts are exactly equal in cents. Among valid pairs,
``scipy.optimize.linear_sum_assignment`` picks the one-to-one alignment with the most matches
(ties broken by description similarity). TP = matched pairs; precision = TP / predicted lines;
recall = TP / gold lines.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

from invoicedesk_evals.metrics.fields import Counts
from invoicedesk_evals.normalize import norm_text

__all__ = ["DESC_THRESHOLD", "Line", "LineAlignment", "align_lines", "description_similarity"]

DESC_THRESHOLD = 0.8


@dataclass(frozen=True, slots=True)
class Line:
    description: str | None
    amount_cents: int | None


@dataclass(frozen=True, slots=True)
class LineAlignment:
    pairs: tuple[tuple[int, int], ...]  # (gold index, predicted index)
    n_gold: int
    n_pred: int

    @property
    def counts(self) -> Counts:
        tp = len(self.pairs)
        return Counts(tp=tp, fp=self.n_pred - tp, fn=self.n_gold - tp)


def description_similarity(a: str | None, b: str | None) -> float:
    na, nb = norm_text(a), norm_text(b)
    if na is None or nb is None:
        return 0.0
    return fuzz.ratio(na, nb) / 100.0


def align_lines(gold: Sequence[Line], pred: Sequence[Line]) -> LineAlignment:
    if not gold or not pred:
        return LineAlignment((), len(gold), len(pred))
    weight = np.zeros((len(gold), len(pred)), dtype=float)
    for i, g in enumerate(gold):
        for j, p in enumerate(pred):
            if g.amount_cents is None or p.amount_cents != g.amount_cents:
                continue
            sim = description_similarity(g.description, p.description)
            if sim >= DESC_THRESHOLD:
                # 1 per match dominates; similarity only breaks ties between matchings.
                weight[i, j] = 1.0 + sim / (len(gold) + len(pred) + 1)
    rows, cols = linear_sum_assignment(weight, maximize=True)
    pairs = tuple((int(i), int(j)) for i, j in zip(rows, cols, strict=True) if weight[i, j] > 0.0)
    return LineAlignment(pairs, len(gold), len(pred))
