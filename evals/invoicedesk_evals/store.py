"""``eval_examples`` / ``eval_runs`` / ``eval_results`` persistence (DESIGN §6.1, migration
0600). Every run writes one ``eval_runs`` row and one ``eval_results`` row per (example,
repeat); ``totals_json`` carries the scorecard shape ``GET /scorecard/latest`` serves."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from invoicedesk.db.connection import Conn
from invoicedesk_data.eval_examples import eval_split_for, gold_json

from invoicedesk_evals.datasets import Example
from invoicedesk_evals.model import DocScore

__all__ = [
    "add_results",
    "ensure_examples",
    "finish_run",
    "latest_run",
    "set_root_causes",
    "start_run",
]


def ensure_examples(conn: Conn, examples: Iterable[Example]) -> dict[str, str]:
    """Upsert one ``eval_examples`` row per example (keyed by ``(split, external_ref)``, the
    data component's migration 0200) and return ``{example_id: row uuid}``."""
    out: dict[str, str] = {}
    for ex in examples:
        row = conn.execute(
            "insert into eval_examples (split, external_ref, gold_json, provenance)"
            " values (%s,%s,%s,'synthetic') on conflict (split, external_ref)"
            " where external_ref is not null do update set gold_json = excluded.gold_json"
            " returning id",
            (eval_split_for(ex.gold), ex.id, json.dumps(gold_json(ex.gold))),
        ).fetchone()
        assert row is not None
        out[ex.id] = str(row["id"])
    return out


def start_run(
    conn: Conn, *, git_sha: str, config: str, split: str, mode: str, n_repeats: int
) -> str:
    row = conn.execute(
        "insert into eval_runs (git_sha, config, split, mode, n_repeats)"
        " values (%s,%s,%s,%s,%s) returning id",
        (git_sha, config, split, mode, n_repeats),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def add_results(
    conn: Conn, run_id: str, example_ids: Mapping[str, str], scores: Iterable[DocScore]
) -> int:
    n = 0
    for s in scores:
        conn.execute(
            "insert into eval_results (run_id, example_id, repeat, metrics_json, cost_usd,"
            " latency_ms, root_cause) values (%s,%s,%s,%s,%s,%s,%s)"
            " on conflict (run_id, example_id, repeat) do update set"
            " metrics_json = excluded.metrics_json, cost_usd = excluded.cost_usd,"
            " latency_ms = excluded.latency_ms, root_cause = excluded.root_cause",
            (
                run_id,
                example_ids[s.example_id],
                s.repeat,
                s.model_dump_json(),
                s.cost_usd,
                s.latency_ms,
                s.root_cause,
            ),
        )
        n += 1
    return n


def set_root_causes(
    conn: Conn, run_id: str, example_ids: Mapping[str, str], labels: Mapping[tuple[str, int], str]
) -> None:
    for (example_id, repeat), label in labels.items():
        conn.execute(
            "update eval_results set root_cause = %s where run_id = %s and example_id = %s"
            " and repeat = %s",
            (label, run_id, example_ids[example_id], repeat),
        )


def finish_run(
    conn: Conn, run_id: str, totals: Mapping[str, Any], *, report_path: str | None = None
) -> None:
    conn.execute(
        "update eval_runs set finished_at = now(), totals_json = %s, report_path = %s"
        " where id = %s",
        (json.dumps(totals, default=str), report_path, run_id),
    )


def latest_run(
    conn: Conn, *, config: str | None = None, mode: str | None = None
) -> dict[str, Any] | None:
    """The most recent finished run (optionally of one config / mode)."""
    row = conn.execute(
        "select id, git_sha, config, split, mode, n_repeats, started_at, finished_at,"
        " totals_json, report_path from eval_runs where finished_at is not null"
        " and (%(config)s::text is null or config = %(config)s)"
        " and (%(mode)s::text is null or mode = %(mode)s)"
        " order by finished_at desc limit 1",
        {"config": config, "mode": mode},
    ).fetchone()
    return None if row is None else dict(row)
