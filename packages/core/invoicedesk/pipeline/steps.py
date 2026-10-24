"""Exactly-once DB effects per pipeline node (migration ``0300_pipeline_steps.sql``).

LangGraph checkpoints after a node returns. If the worker dies after a node committed its DB
writes but before the checkpoint was saved, LangGraph re-runs that node on resume. Every node
therefore writes its effects, its status transition and a ``pipeline_steps`` row in one
transaction (holding the document row lock), and checks for that row first: a re-run returns
the stored output and writes nothing.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from invoicedesk.db.connection import Conn, Database

__all__ = ["Step", "load_step", "load_step_conn", "save_step"]

Step = Literal["preprocess", "screen", "extract", "validate", "post"]


def load_step_conn(
    conn: Conn, document_id: str, step: Step, round_: int = 0
) -> dict[str, Any] | None:
    row = conn.execute(
        "select output_json from pipeline_steps where document_id = %s and step = %s"
        " and round = %s",
        (document_id, step, round_),
    ).fetchone()
    return None if row is None else dict(row["output_json"])


def load_step(db: Database, document_id: str, step: Step, round_: int = 0) -> dict[str, Any] | None:
    with db.tx() as conn:
        return load_step_conn(conn, document_id, step, round_)


def save_step(
    conn: Conn, document_id: str, step: Step, output: dict[str, Any], round_: int = 0
) -> None:
    """Insert the marker inside the caller's transaction (the node's effects commit with it)."""
    conn.execute(
        "insert into pipeline_steps (document_id, step, round, output_json) values (%s,%s,%s,%s)",
        (document_id, step, round_, json.dumps(output)),
    )
