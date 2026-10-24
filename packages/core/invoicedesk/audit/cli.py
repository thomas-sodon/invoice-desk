"""``invoicedesk audit-verify``: recompute the audit hash chain."""

from __future__ import annotations

import argparse

from invoicedesk.audit.chain import verify
from invoicedesk.db.connection import Database


def main() -> int:
    from invoicedesk.config.settings import get_settings

    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=None)
    a = ap.parse_args()
    with Database.connect(a.dsn or get_settings().database_url) as conn:
        ok, bad = verify(conn)
    print("audit chain OK" if ok else f"audit chain BROKEN at id {bad}")
    return 0 if ok else 1
