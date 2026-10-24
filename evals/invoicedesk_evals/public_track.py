"""Public-benchmark track (M18): SROIE Task 3 and the CORD v1 test split, so at least one
headline number is on data this project did not author.

The images are downloaded by ``invoicedesk-data public sroie|cord`` into the gitignored cache
(``data/public/cache``) and never redistributed. When the cache is absent the track is skipped.
Each receipt runs through the production reader path (preprocess -> screening -> tiered
extraction; no rules, since receipts have no work orders), and the mapped fields are scored with
the same normalizers as the synthetic track: SROIE vendor name, address, date and total; CORD
total only (CORD has no store or date labels). Receipts are not invoices, so this measures
reading, not routing. Results are published honestly with no target.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.llm import LLMClient
from invoicedesk.contracts.preprocess import Preprocessor
from invoicedesk_data.paths import PUBLIC_CACHE_DIR
from invoicedesk_data.public.load import load_public
from invoicedesk_data.public.mappings import PublicRecord, to_gold_extraction

from invoicedesk_evals.harness import ConfigLLM, LockedStorage
from invoicedesk_evals.matrix import ConfigSpec
from invoicedesk_evals.metrics.fields import Counts, score_header_fields
from invoicedesk_evals.stats import bootstrap_ci

__all__ = ["PUBLIC_FIELDS", "run_public_source"]

PUBLIC_FIELDS: dict[str, tuple[str, ...]] = {
    "sroie": ("vendor_name", "vendor_address", "invoice_date", "total"),
    "cord": ("total",),
}

_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _counts_from(doc: dict[str, Any]) -> Counts:
    c = Counts()
    for f in doc["fields"].values():
        c = c + Counts(f["tp"], f["fp"], f["fn"])
    return c


def _f1(docs: Sequence[dict[str, Any]]) -> dict[str, float | None]:
    c = Counts()
    for d in docs:
        c = c + _counts_from(d)
    return {"field_f1": c.f1}


def run_public_source(
    source: str,
    spec: ConfigSpec,
    llm: LLMClient,
    *,
    cache: Path = PUBLIC_CACHE_DIR,
    limit: int | None = None,
    preprocessor: Preprocessor | None = None,
    storage: Storage | None = None,
    n_resamples: int = 1000,
    seed: int = 0,
) -> dict[str, Any]:
    """Score one public source. Returns ``{"source", "skipped"}`` when the cache is absent."""
    records: list[PublicRecord] = load_public(source, cache)
    if limit:
        records = records[:limit]
    if not records:
        return {"source": source, "skipped": f"no {source} cache at {cache / source}"}
    from invoicedesk.calibrate import load_calibrator
    from invoicedesk.extract import ExtractionFailed, build_extractor
    from invoicedesk.guardrails import build_screeners, run_screening

    storage = storage or LockedStorage()  # must be the preprocessor's render storage
    meter = ConfigLLM(llm, spec, simulated_latency=False)
    pre = preprocessor
    if pre is None:
        from invoicedesk.preprocess import build_preprocessor

        pre = build_preprocessor(storage, key_prefix="public")
    screeners = build_screeners(meter, storage=storage)
    extractor = build_extractor(meter, calibrator=load_calibrator(), storage=storage)
    fields = PUBLIC_FIELDS[source]
    docs: list[dict[str, Any]] = []
    for r in records:
        path = cache / source / r.image
        if not path.exists():
            continue
        doc_id = str(uuid.uuid4())
        t0 = time.perf_counter()
        status = "extracted"
        pred: InvoiceExtraction | None = None
        try:
            p = pre.preprocess(
                path.read_bytes(),
                mime=_MIME.get(path.suffix.lower(), "image/jpeg"),
                document_id=doc_id,
                filename=path.name,
            )
            screening = run_screening(p, screeners, email_body=None)
            if screening.quarantined:
                status = "quarantined"
            else:
                pred = extractor.extract(p, screening=screening, max_tier=spec.max_tier).final
        except ExtractionFailed:
            status = "failed"
        except Exception as exc:  # a public receipt the preprocessor rejects, etc.
            status = f"error:{type(exc).__name__}"
        calls = meter.calls_for(doc_id)
        outcomes = score_header_fields(to_gold_extraction(r), pred, fields=fields)
        docs.append(
            {
                "id": r.id,
                "status": status,
                "fields": {
                    n: {"tp": o.counts.tp, "fp": o.counts.fp, "fn": o.counts.fn}
                    for n, o in outcomes.items()
                },
                "cost_usd": str(sum((Decimal(c.cost_usd) for c in calls), Decimal(0))),
                "wall_ms": int((time.perf_counter() - t0) * 1000),
            }
        )
    if not docs:
        return {"source": source, "skipped": "index present but no images on disk"}
    total = Counts()
    per_field: dict[str, Counts] = {}
    for d in docs:
        for n, f in d["fields"].items():
            c = Counts(f["tp"], f["fp"], f["fn"])
            per_field[n] = per_field.get(n, Counts()) + c
            total = total + c
    ci = bootstrap_ci(docs, _f1, key=lambda d: str(d["id"]), n_resamples=n_resamples, seed=seed)
    usd = sum((Decimal(d["cost_usd"]) for d in docs), Decimal(0))
    f1_ci = ci.get("field_f1")
    return {
        "source": source,
        "n_docs": len(docs),
        "config": spec.name,
        "fields": list(fields),
        "field_f1": {"value": total.f1, "ci": list(f1_ci) if f1_ci is not None else None},
        "per_field": {
            n: {"precision": c.precision, "recall": c.recall, "f1": c.f1}
            for n, c in per_field.items()
        },
        "status_counts": {
            s: sum(d["status"] == s for d in docs) for s in sorted({d["status"] for d in docs})
        },
        "usd_per_doc": str(usd / len(docs)),
        "docs": docs,
    }
