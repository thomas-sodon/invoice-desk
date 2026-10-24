"""Eval example sets (DESIGN §5.6) over the committed gold labels in ``data/gold``.

Splits:

* ``dev`` / ``test``: the frozen 60/60 halves of ``synthetic-core``. Thresholds, prompts and the
  calibrator are tuned on ``dev`` only (CLAUDE.md); ``test`` is only ever *scored*.
* ``adversarial``: all 38 adversarial-set documents (30 cases + 8 ``prior`` documents that the
  duplicate cases need).
* ``adversarial-cases``: the 30 cases only (priors still run first, as support documents).
* ``smoke``: the PR-gate set: a 30-document stratified dev subset plus the 30 adversarial cases.
* ``all``: dev + test + adversarial (the nightly full set).

Specs can be combined with ``+`` (``dev+adversarial``). The demo batch (``data/demo``) is never
part of any split.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from invoicedesk_data.gold import GoldRecord, file_path, load_gold
from invoicedesk_data.paths import GOLD_DIR

from invoicedesk_evals.model import EvalExample

__all__ = [
    "SMOKE_SEED",
    "SMOKE_SIZE",
    "TEST_SPLIT",
    "Example",
    "assert_not_test",
    "load_split",
    "smoke_dev_ids",
    "split_names",
]

TEST_SPLIT = "test"
SMOKE_SIZE = 30
SMOKE_SEED = "invoicedesk-smoke-v1"
_SETS = {"dev": "synthetic-core", "test": "synthetic-core", "adversarial": "adversarial"}
KNOWN = ("dev", "test", "adversarial", "adversarial-cases", "smoke", "all")


@dataclass(frozen=True)
class Example:
    example: EvalExample
    gold: GoldRecord
    #: Gold records processed before this one in the same workspace (duplicate priors).
    support: tuple[GoldRecord, ...] = ()

    @property
    def id(self) -> str:
        return self.example.example_id

    def file(self, base: Path | None = None) -> Path:
        return file_path(self.gold, base=base)


def assert_not_test(split: str, what: str = "tune") -> None:
    """Thresholds, prompts and calibrators are tuned on dev only (CLAUDE.md, Evals)."""
    parts = {p.strip() for p in split.split("+")}
    if TEST_SPLIT in parts or "all" in parts:
        raise PermissionError(
            f"refusing to {what} on split {split!r}: the test split is frozen and never used "
            "for tuning; use --split dev"
        )


def split_names(spec: str) -> list[str]:
    parts = [p.strip() for p in spec.split("+") if p.strip()]
    for p in parts:
        if p not in KNOWN:
            raise ValueError(f"unknown split {p!r}; known: {', '.join(KNOWN)}")
    return parts


def _example(r: GoldRecord) -> EvalExample:
    split = r.split if r.set == "synthetic-core" else "adversarial"
    assert split is not None
    return EvalExample(
        example_id=f"{r.set}/{r.id}",
        doc_id=r.id,
        set=r.set,
        split=split,
        medium=r.medium,
        category=r.category,
        priors=[f"{r.set}/{p}" for p in r.priors],
    )


def _with_support(records: Sequence[GoldRecord], pool: dict[str, GoldRecord]) -> list[Example]:
    out: list[Example] = []
    for r in records:
        support: list[GoldRecord] = []
        seen: set[str] = set()

        def add(
            rec: GoldRecord, support: list[GoldRecord] = support, seen: set[str] = seen
        ) -> None:
            for p in rec.priors:
                if p in seen:
                    continue
                seen.add(p)
                prior = pool[p]
                add(prior)
                support.append(prior)

        add(r)
        out.append(Example(_example(r), r, tuple(support)))
    return out


def smoke_dev_ids(dev: Sequence[GoldRecord], size: int = SMOKE_SIZE) -> list[str]:
    """Deterministic stratified subset of dev: proportional by medium (largest remainder),
    and within each medium alternating held / clean labels so both routes are covered."""
    by_medium: dict[str, list[GoldRecord]] = {}
    for r in dev:
        by_medium.setdefault(r.medium, []).append(r)
    n = len(dev)
    quotas = {m: size * len(rs) / n for m, rs in by_medium.items()}
    alloc = {m: int(q) for m, q in quotas.items()}
    for m in sorted(quotas, key=lambda k: (-(quotas[k] - alloc[k]), k))[
        : size - sum(alloc.values())
    ]:
        alloc[m] += 1

    def rank(r: GoldRecord) -> str:
        return hashlib.sha256(f"{SMOKE_SEED}:{r.id}".encode()).hexdigest()

    chosen: list[str] = []
    for m in sorted(by_medium):
        clean = sorted(
            (r for r in by_medium[m] if str(r.expected.status) == "ReadyToPay"), key=rank
        )
        held = sorted((r for r in by_medium[m] if str(r.expected.status) != "ReadyToPay"), key=rank)
        picked: list[GoldRecord] = []
        while len(picked) < alloc[m] and (clean or held):
            # roughly the dev ratio of held:clean (~1:2), but at least one held per medium
            src = held if held and (len(picked) % 3 == 0) else (clean or held)
            picked.append(src.pop(0))
        chosen.extend(r.id for r in picked)
    return chosen


def load_split(spec: str, *, gold_dir: Path = GOLD_DIR) -> list[Example]:
    """Examples for a split spec, in manifest order, de-duplicated."""
    parts = split_names(spec)
    want: list[GoldRecord] = []
    core = {r.id: r for r in load_gold("synthetic-core", gold_dir=gold_dir)}
    adv_list = load_gold("adversarial", gold_dir=gold_dir)
    adv = {r.id: r for r in adv_list}
    for p in parts:
        if p in ("dev", "test"):
            want += [r for r in core.values() if r.split == p]
        elif p == "adversarial":
            want += adv_list
        elif p == "adversarial-cases":
            want += [r for r in adv_list if r.category not in (None, "prior")]
        elif p == "smoke":
            dev = [r for r in core.values() if r.split == "dev"]
            ids = set(smoke_dev_ids(dev))
            want += [r for r in dev if r.id in ids]
            want += [r for r in adv_list if r.category not in (None, "prior")]
        elif p == "all":
            want += [r for r in core.values() if r.split in ("dev", "test")] + adv_list
    seen: set[str] = set()
    uniq: list[GoldRecord] = []
    for r in want:
        key = f"{r.set}/{r.id}"
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    pool = {**core, **adv}
    return _with_support(uniq, pool)


def filter_media(examples: Iterable[Example], media: Sequence[str] | None) -> list[Example]:
    if not media:
        return list(examples)
    return [e for e in examples if e.example.medium in media]
