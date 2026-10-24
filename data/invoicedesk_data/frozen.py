"""Checks on the committed gold: frozen-split integrity and the rules self-check."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from invoicedesk_data.gold import GoldRecord, load_gold, load_manifest, sha256_hex
from invoicedesk_data.paths import GOLD_DIR
from invoicedesk_data.universe import build_universe
from invoicedesk_data.verify import RouteCheck, check_truth, prior_from

__all__ = ["self_check", "verify_frozen"]


def verify_frozen(gold_dir: Path = GOLD_DIR) -> list[str]:
    """Problems with the frozen synthetic-core split (empty list = OK).

    * the manifest is frozen, 60 dev / 60 test, stratified 40/40/25/15;
    * every gold file matches the hash recorded in the manifest (no edited labels);
    * the generator still reproduces every committed truth and split assignment.
    """
    from invoicedesk_data.splits import CORE_COUNTS, CORE_SET, assign_splits, core_truths

    problems: list[str] = []
    m = load_manifest(CORE_SET, gold_dir)
    if not m.frozen:
        problems.append("manifest is not marked frozen")
    splits = Counter(e.split for e in m.entries)
    if splits != Counter({"dev": 60, "test": 60}):
        problems.append(f"split sizes {dict(splits)} != 60 dev / 60 test")
    media = Counter(e.medium for e in m.entries)
    if media != Counter(CORE_COUNTS):
        problems.append(f"media {dict(media)} != {CORE_COUNTS}")
    records: dict[str, GoldRecord] = {}
    for e in m.entries:
        path = gold_dir / CORE_SET / f"{e.id}.json"
        rec = GoldRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if rec.gold_sha256() != e.gold_sha256:
            problems.append(f"{e.id}: gold file differs from the manifest hash (edited label?)")
        if rec.split != e.split:
            problems.append(f"{e.id}: split {rec.split} != manifest {e.split}")
        records[e.id] = rec
    truths = core_truths()
    expected_split = assign_splits(truths)
    for t in truths:
        rec = records.get(t.doc_id)
        if rec is None:
            problems.append(f"{t.doc_id}: missing from the manifest")
            continue
        if rec.truth.model_dump(mode="json", exclude={"params"}) != t.model_dump(
            mode="json", exclude={"params"}
        ):
            problems.append(f"{t.doc_id}: generator no longer reproduces the committed truth")
        if expected_split[t.doc_id] != rec.split:
            problems.append(f"{t.doc_id}: split assignment drifted")
    return problems


def self_check(set_name: str, gold_dir: Path = GOLD_DIR) -> list[RouteCheck]:
    """Route each gold record (with its priors) through the real rules engine."""
    td = build_universe().to_tenant_data()
    records = load_gold(set_name, gold_dir=gold_dir)
    by_id = {r.id: r for r in records}
    out: list[RouteCheck] = []
    batch_priors = []
    for r in records:
        priors = [prior_from(by_id[p].truth, sha256=by_id[p].sha256) for p in r.priors]
        if set_name == "demo" and r.category != "email":
            priors = priors + batch_priors  # a batch is processed in board order
        out.append(check_truth(r.truth, td, email=r.email, priors=priors))
        if set_name == "demo" and r.category != "email":
            batch_priors.append(prior_from(r.truth, sha256=r.sha256))
    return out


def gold_hash(path: Path) -> str:
    return sha256_hex(path.read_bytes())
