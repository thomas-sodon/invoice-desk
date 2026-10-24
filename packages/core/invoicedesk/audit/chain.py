"""Hash chain over ``audit_log``.

``row_hash = sha256(canonical_json({ts, actor, tenant_id, document_id, action, before, after,
prev_hash}))`` and each row's ``prev_hash`` is the previous row's ``row_hash`` (genesis = 64
zeros). Payloads are masked (last-4) BEFORE hashing and storing. A transaction-scoped advisory
lock serializes appends so the chain never forks. The DB trigger forbids UPDATE/DELETE/TRUNCATE.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg

from invoicedesk.masking import mask_payload

__all__ = ["GENESIS_HASH", "AuditEntry", "append", "canonical_json", "row_hash", "verify"]

GENESIS_HASH = "0" * 64
_LOCK_ID = 727270002


def _default(o: object) -> str:
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    return str(o)


def canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def _ts_str(ts: dt.datetime) -> str:
    return ts.astimezone(dt.UTC).isoformat()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    ts: dt.datetime
    actor: str
    tenant_id: str | None
    document_id: str | None
    action: str
    before: Any
    after: Any
    prev_hash: str
    row_hash: str


def row_hash(
    *,
    ts: dt.datetime,
    actor: str,
    tenant_id: str | None,
    document_id: str | None,
    action: str,
    before: Any,
    after: Any,
    prev_hash: str,
) -> str:
    body = canonical_json(
        {
            "ts": _ts_str(ts),
            "actor": actor,
            "tenant_id": tenant_id,
            "document_id": str(document_id) if document_id is not None else None,
            "action": action,
            "before": before,
            "after": after,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def append(
    conn: psycopg.Connection[Any],
    *,
    actor: str,
    action: str,
    document_id: str | None = None,
    tenant_id: str | None = None,
    before: Any = None,
    after: Any = None,
    ts: dt.datetime | None = None,
) -> AuditEntry:
    """Append one row inside the caller's transaction. Payloads are masked and normalized."""
    before_n = json.loads(canonical_json(mask_payload(before))) if before is not None else None
    after_n = json.loads(canonical_json(mask_payload(after))) if after is not None else None
    ts = (ts or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    with conn.cursor() as cur:
        cur.execute("select pg_advisory_xact_lock(%s)", (_LOCK_ID,))
        cur.execute("select row_hash from audit_log order by id desc limit 1")
        last = cur.fetchone()
        prev = _first(last) if last is not None else GENESIS_HASH
        h = row_hash(
            ts=ts,
            actor=actor,
            tenant_id=tenant_id,
            document_id=document_id,
            action=action,
            before=before_n,
            after=after_n,
            prev_hash=prev,
        )
        cur.execute(
            "insert into audit_log (ts, actor, tenant_id, document_id, action, before_json,"
            " after_json, prev_hash, row_hash) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
            (
                ts,
                actor,
                tenant_id,
                document_id,
                action,
                None if before_n is None else canonical_json(before_n),
                None if after_n is None else canonical_json(after_n),
                prev,
                h,
            ),
        )
        new_id = _first(cur.fetchone())
    return AuditEntry(
        int(new_id), ts, actor, tenant_id, document_id, action, before_n, after_n, prev, h
    )


def _first(row: Any) -> Any:
    if row is None:
        raise RuntimeError("expected a row")
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def verify(conn: psycopg.Connection[Any]) -> tuple[bool, int | None]:
    """Recompute the whole chain. Returns ``(True, None)`` or ``(False, first_bad_id)``."""
    prev = GENESIS_HASH
    with conn.cursor() as cur:
        cur.execute(
            "select id, ts, actor, tenant_id, document_id, action, before_json, after_json,"
            " prev_hash, row_hash from audit_log order by id"
        )
        for r in cur:
            vals = list(r.values()) if isinstance(r, dict) else list(r)
            rid, ts, actor, tenant_id, doc_id, action, before, after, prev_hash, rh = vals
            expected = row_hash(
                ts=ts,
                actor=actor,
                tenant_id=tenant_id,
                document_id=str(doc_id) if doc_id is not None else None,
                action=action,
                before=before,
                after=after,
                prev_hash=prev,
            )
            if prev_hash != prev or rh != expected:
                return False, int(rid)
            prev = rh
    return True, None
