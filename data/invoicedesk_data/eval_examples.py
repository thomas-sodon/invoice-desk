"""Load committed gold labels into ``eval_examples`` (idempotent; migration 0200)."""

from __future__ import annotations

import json
from collections.abc import Iterable

from invoicedesk.db.connection import Database

from invoicedesk_data.gold import GoldRecord, load_gold
from invoicedesk_data.paths import GOLD_DIR

__all__ = ["eval_split_for", "gold_json", "load_eval_examples"]


def eval_split_for(record: GoldRecord) -> str:
    """``eval_split`` enum value: synthetic-core dev/test, ``adversarial`` or ``demo``."""
    if record.set == "synthetic-core":
        assert record.split in ("dev", "test"), record.split
        return record.split
    return "adversarial" if record.set == "adversarial" else "demo"


def gold_json(record: GoldRecord) -> dict[str, object]:
    return {
        "extraction": record.extraction,
        "expected": record.expected.model_dump(mode="json"),
        "vendor_id": record.truth.vendor_id,
        "medium": record.medium,
        "box_eval": record.box_eval,
        "boxes": {k: v.model_dump(mode="json") for k, v in record.boxes.items()},
        "hidden_boxes": [b.model_dump(mode="json") for b in record.hidden_boxes],
        "filename": record.filename,
        "sha256": record.sha256,
        "priors": record.priors,
        "category": record.category,
        "email": record.email.model_dump(mode="json") if record.email else None,
    }


def load_eval_examples(
    db: Database,
    sets: Iterable[str] = ("synthetic-core", "adversarial", "demo"),
    *,
    gold_dir=GOLD_DIR,
) -> int:
    """Insert (or refresh) one ``eval_examples`` row per gold record. Returns rows written."""
    n = 0
    with db.tx() as conn:
        for s in sets:
            if not (gold_dir / s / "manifest.json").exists():
                continue
            for r in load_gold(s, gold_dir=gold_dir):
                conn.execute(
                    "insert into eval_examples (split, external_ref, gold_json, provenance)"
                    " values (%s,%s,%s,'synthetic') on conflict (split, external_ref)"
                    " where external_ref is not null do update set gold_json = excluded.gold_json",
                    (eval_split_for(r), f"{s}/{r.id}", json.dumps(gold_json(r))),
                )
                n += 1
    return n
