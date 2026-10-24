"""Apply ``infra/supabase/migrations/*.sql`` in filename order, tracked in schema_migrations.

Component migrations use their assigned number range (see docs/foundation.md), e.g.
``0200_pipeline_checkpoints.sql``. Files are applied once each, in lexical order.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import LiteralString, cast

import psycopg

__all__ = ["MIGRATIONS_DIR", "apply_migrations", "main"]

MIGRATIONS_DIR = Path(__file__).resolve().parents[4] / "infra" / "supabase" / "migrations"


def apply_migrations(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "create table if not exists schema_migrations "
            "(filename text primary key, applied_at timestamptz not null default now())"
        )
        conn.execute("select pg_advisory_lock(727270001)")
        try:
            done = {r[0] for r in conn.execute("select filename from schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(cast("LiteralString", path.read_text(encoding="utf-8")))
                    conn.execute(
                        "insert into schema_migrations (filename) values (%s)", (path.name,)
                    )
                applied.append(path.name)
        finally:
            conn.execute("select pg_advisory_unlock(727270001)")
    return applied


def main() -> None:
    from invoicedesk.config.settings import get_settings

    ap = argparse.ArgumentParser(description="Apply InvoiceDesk SQL migrations")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()
    for name in apply_migrations(args.dsn or get_settings().database_url):
        print(f"applied {name}")


if __name__ == "__main__":
    main()
