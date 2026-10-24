"""``synthetic-core``: 120 labeled invoices, frozen 60 dev / 60 test (DESIGN §5.6, §6.2).

Stratification: native PDF 40, scanned 40, phone photo 25, handwritten 15. 36 documents carry a
planted defect (two of them the deliberately illegible handwritten totals); 84 are clean. The
split is stratified by medium and by clean/defect, deterministically from :data:`CORE_SEED`.

Gold labels and the manifest are committed in ``data/gold/synthetic-core/``; binaries go to
``data/generated/synthetic-core/`` and are rebuilt with ``invoicedesk-data generate``. The
manifest is ``frozen``: tests assert the truths regenerate to the committed gold and that no
committed gold file differs from its manifest hash. CLAUDE.md: never look at, tune on or edit
the test split.
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

from invoicedesk_data.generator.build import GENERATOR_VERSION, GeneratedDoc, build_document
from invoicedesk_data.generator.render import Renderer
from invoicedesk_data.generator.sampler import TruthSampler
from invoicedesk_data.generator.truth import InvoiceTruth, Medium
from invoicedesk_data.gold import Manifest, write_set
from invoicedesk_data.paths import GENERATED_DIR, GOLD_DIR, REPO_ROOT
from invoicedesk_data.universe import AS_OF, Universe, build_universe

__all__ = [
    "CORE_COUNTS",
    "CORE_DEFECTS",
    "CORE_SEED",
    "CORE_SET",
    "CORE_WINDOW",
    "assign_splits",
    "build_core",
    "core_truths",
]

CORE_SET = "synthetic-core"
CORE_SEED = 1_202_610
CORE_WINDOW = (dt.date(2026, 7, 25), dt.date(2026, 9, 25))
CORE_COUNTS: dict[Medium, int] = {"native": 40, "scanned": 40, "photo": 25, "handwritten": 15}
CORE_DEFECTS: dict[str, int] = {
    "over_estimate": 6,
    "wrong_unit": 3,
    "unknown_wo": 3,
    "missing_wo": 3,
    "closed_wo": 2,
    "arithmetic": 4,
    "stale_date": 3,
    "due_before_invoice": 2,
    "over_cap": 3,
    "new_vendor": 3,
    "pending_vendor": 2,
    "illegible_total": 2,
}


def _plan(rng: random.Random) -> list[tuple[Medium, str | None]]:
    media: list[Medium] = [m for m, n in CORE_COUNTS.items() for _ in range(n)]
    defects = [d for d, n in CORE_DEFECTS.items() for _ in range(n) if d != "illegible_total"]
    plan: list[tuple[Medium, str | None]] = [(m, None) for m in media]
    hand = [i for i, (m, _) in enumerate(plan) if m == "handwritten"]
    for i in rng.sample(hand, CORE_DEFECTS["illegible_total"]):
        plan[i] = (plan[i][0], "illegible_total")
    free = [i for i, (_, d) in enumerate(plan) if d is None]
    for i, d in zip(rng.sample(free, len(defects)), defects, strict=True):
        plan[i] = (plan[i][0], d)
    return plan


def core_truths(universe: Universe | None = None) -> list[InvoiceTruth]:
    """The 120 truths, in id order (fast: no rendering)."""
    u = universe or build_universe()
    rng = random.Random(CORE_SEED)  # noqa: S311 - reproducible synthetic data
    sampler = TruthSampler(
        u,
        seed=CORE_SEED + 1,
        set_name=CORE_SET,
        prefix="core",
        window=CORE_WINDOW,
        number_offset=300,
    )
    out: list[InvoiceTruth] = []
    for medium, defect in _plan(rng):
        if defect is None:
            out.append(sampler.clean(medium))
        else:
            out.append(sampler.with_defect(defect, medium))
    return out


def assign_splits(truths: list[InvoiceTruth], seed: int = CORE_SEED) -> dict[str, str]:
    """Stratified by medium and clean/defect: exactly 60 dev / 60 test."""
    rng = random.Random(seed + 7)  # noqa: S311 - reproducible synthetic data
    split: dict[str, str] = {}
    for medium in CORE_COUNTS:
        group = [t for t in truths if t.medium == medium]
        bad = [t.doc_id for t in group if t.defects]
        good = [t.doc_id for t in group if not t.defects]
        rng.shuffle(bad)
        rng.shuffle(good)
        parity = 1 if medium == "handwritten" else 0
        for i, doc_id in enumerate(bad + good):
            split[doc_id] = "dev" if i % 2 == parity else "test"
    return split


def build_core(
    out_dir: Path = GENERATED_DIR / CORE_SET,
    *,
    gold_dir: Path = GOLD_DIR,
    use_augraphy: bool = False,
    limit: int | None = None,
) -> tuple[Manifest, list[GeneratedDoc]]:
    truths = core_truths()
    splits = assign_splits(truths)
    if limit is not None:
        # A partial build must never overwrite the frozen, committed gold (it would delete the
        # other labels, test split included): its gold goes next to the partial binaries.
        truths = truths[:limit]
        if gold_dir == GOLD_DIR:
            gold_dir = out_dir / "partial-gold"
    out_dir.mkdir(parents=True, exist_ok=True)
    docs: list[GeneratedDoc] = []
    with Renderer() as r:
        for t in truths:
            d = build_document(t, r, split=splits[t.doc_id], use_augraphy=use_augraphy)
            (out_dir / d.filename).write_bytes(d.data)
            docs.append(d)
    manifest = write_set(
        CORE_SET,
        [d.gold for d in docs],
        seed=CORE_SEED,
        as_of=AS_OF.isoformat(),
        generator_version=GENERATOR_VERSION,
        files_dir=str(out_dir.relative_to(REPO_ROOT))
        if out_dir.is_relative_to(REPO_ROOT)
        else str(out_dir),
        frozen=limit is None,
        gold_dir=gold_dir,
    )
    return manifest, docs
