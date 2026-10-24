#!/usr/bin/env python3
"""Delete try-it visitor uploads older than 24h (M14), with their storage objects.

    uv run python scripts/purge_visitor_uploads.py            # purge now (make purge)
    uv run python scripts/purge_visitor_uploads.py --dry-run  # list what would go

Run it hourly (cron, Fly.io scheduled machine, or an n8n schedule). Audit rows survive: they
have no foreign key, and each purged document gets a ``document.purged`` row.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="purge_visitor_uploads", description=__doc__)
    ap.add_argument("--dsn", default=None, help="Postgres DSN (default: DATABASE_URL)")
    ap.add_argument("--dry-run", action="store_true", help="only count expired uploads")
    ap.add_argument("--limit", type=int, default=500, help="max documents per run")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from invoicedesk import wiring
    from invoicedesk.config.settings import get_settings
    from invoicedesk.db.connection import Database
    from invoicedesk_api.purge import purge_expired

    settings = get_settings()
    with Database(args.dsn or settings.database_url, max_size=2) as db:
        if args.dry_run:
            with db.tx() as conn:
                row = conn.execute(
                    "select count(*) as n from documents where session_id is not null"
                    " and purge_after < %s",
                    (dt.datetime.now(dt.UTC),),
                ).fetchone()
            n = 0 if row is None else int(row["n"])
            print(f"{n} expired visitor upload(s) would be purged")
            return 0
        report = purge_expired(db, wiring.storage(settings), limit=args.limit)
    print(
        f"purged {len(report.documents)} document(s), {report.objects_deleted} object(s),"
        f" {report.batches_deleted} empty try-it batch(es); skipped {len(report.skipped)}"
    )
    return 1 if report.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
