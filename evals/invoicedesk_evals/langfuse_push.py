"""Push nightly eval results to Langfuse datasets (DESIGN §5.6 nightly job), when configured.

Uses the Langfuse public REST API with basic auth (public key / secret key):

1. ``POST /api/public/datasets``: create ``invoicedesk-<split>`` (idempotent; 409 is fine);
2. ``POST /api/public/dataset-items``: one item per example (upsert by ``id``), carrying the
   example id and expected status only (no document content);
3. ``POST /api/public/ingestion``: one trace per (config, example, repeat) with its outcome,
   plus scores (``field_f1``, ``false_stp``, ``adversarial_miss``, ``cost_usd``);
4. ``POST /api/public/dataset-run-items``: links each trace to its dataset item under the run
   name ``<config>-<run id prefix>``.

Nothing is sent unless ``LANGFUSE_HOST``, ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY``
are set. Payloads are masked (``mask_payload``) before they leave the process.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Sequence
from typing import Any

import httpx
from invoicedesk.config.settings import Settings
from invoicedesk.masking import mask_payload

from invoicedesk_evals.model import DocScore

__all__ = ["langfuse_configured", "push_results"]


def langfuse_configured(s: Settings) -> bool:
    return bool(s.langfuse_host and s.langfuse_public_key and s.langfuse_secret_key)


def _item_id(split: str, example_id: str) -> str:
    return "invoicedesk-" + hashlib.sha256(f"{split}:{example_id}".encode()).hexdigest()[:32]


def _doc_f1(s: DocScore) -> float | None:
    tp = sum(f.tp for f in s.fields.values())
    d = 2 * tp + sum(f.fp + f.fn for f in s.fields.values())
    return 2 * tp / d if d else None


def push_results(
    settings: Settings,
    *,
    run_id: str,
    config: str,
    split: str,
    scores: Sequence[DocScore],
    client: httpx.Client | None = None,
) -> dict[str, int]:
    if not langfuse_configured(settings):
        return {"skipped": 1}
    assert settings.langfuse_host
    assert settings.langfuse_public_key
    assert settings.langfuse_secret_key is not None
    base = settings.langfuse_host.rstrip("/")
    auth = (settings.langfuse_public_key, settings.langfuse_secret_key.get_secret_value())
    http = client or httpx.Client(timeout=30.0)
    dataset = f"invoicedesk-{split.replace('+', '-')}"
    r = http.post(f"{base}/api/public/datasets", auth=auth, json={"name": dataset})
    if r.status_code not in (200, 201, 409):
        r.raise_for_status()
    items = 0
    for ex_id in sorted({s.example_id for s in scores}):
        first = next(s for s in scores if s.example_id == ex_id)
        http.post(
            f"{base}/api/public/dataset-items",
            auth=auth,
            json={
                "datasetName": dataset,
                "id": _item_id(split, ex_id),
                "input": {"example_id": ex_id, "medium": first.medium},
                "expectedOutput": {"status": first.expected_status},
            },
        ).raise_for_status()
        items += 1
    now = dt.datetime.now(dt.UTC).isoformat()
    batch: list[dict[str, Any]] = []
    links: list[tuple[str, str]] = []
    for s in scores:
        trace_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}/{s.example_id}/{s.repeat}"))
        out = mask_payload(
            {
                "status": s.status,
                "auto_posted": s.auto_posted,
                "quarantined": s.quarantined,
                "rules_failed": s.rules_failed,
            }
        )
        batch.append(
            {
                "id": str(uuid.uuid4()),
                "type": "trace-create",
                "timestamp": now,
                "body": {
                    "id": trace_id,
                    "name": f"eval {config}",
                    "input": {"example_id": s.example_id, "repeat": s.repeat},
                    "output": out,
                    "metadata": {"run_id": run_id, "config": config, "split": split},
                    "tags": ["invoicedesk-eval", config],
                },
            }
        )
        values: dict[str, float | None] = {
            "field_f1": _doc_f1(s),
            "false_stp": float(s.false_stp),
            "adversarial_miss": float(s.adversarial_miss),
            "cost_usd": float(s.cost_usd),
        }
        for name, v in values.items():
            if v is None:
                continue
            batch.append(
                {
                    "id": str(uuid.uuid4()),
                    "type": "score-create",
                    "timestamp": now,
                    "body": {
                        "id": str(uuid.uuid4()),
                        "traceId": trace_id,
                        "name": name,
                        "value": v,
                    },
                }
            )
        links.append((trace_id, _item_id(split, s.example_id)))
    for i in range(0, len(batch), 200):
        http.post(
            f"{base}/api/public/ingestion", auth=auth, json={"batch": batch[i : i + 200]}
        ).raise_for_status()
    run_name = f"{config}-{run_id[:8]}"
    for trace_id, item_id in links:
        http.post(
            f"{base}/api/public/dataset-run-items",
            auth=auth,
            json={"runName": run_name, "datasetItemId": item_id, "traceId": trace_id},
        ).raise_for_status()
    return {"items": items, "traces": len(links)}
