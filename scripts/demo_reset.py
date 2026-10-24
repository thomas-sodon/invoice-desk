#!/usr/bin/env python3
"""Reset the demo: delete demo bills in the QBO sandbox, clear demo batches, re-seed the board.

Dry run by default; pass ``--apply`` to change anything (``make demo-reset APPLY=1``).

    uv run python scripts/demo_reset.py              # show what would be reset
    uv run python scripts/demo_reset.py --apply      # do it
    uv run python scripts/demo_reset.py --apply --sweep   # also delete untracked InvoiceDesk bills

QBO deletes go through ``invoicedesk.accounting.sandbox_admin`` (a demo-only helper, separate
from the least-privilege posting adapter). Without QBO credentials (offline mode) the sandbox
step needs ``--skip-qbo``. ``audit_log`` rows are never deleted; the reset appends a
``demo.reset`` row.
"""

from __future__ import annotations

import argparse
import logging
import sys

from invoicedesk.config.settings import Settings, get_settings


def _admin(settings: Settings) -> object | None:
    if settings.mode != "live" or not settings.qbo_client_id:
        return None
    from invoicedesk.accounting import build_qbo_transport
    from invoicedesk.accounting.sandbox_admin import QboSandboxAdmin

    return QboSandboxAdmin(build_qbo_transport(settings))


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="actually reset (default: dry run)")
    ap.add_argument("--tenant", default=None, help="tenant id (default: INVOICEDESK_TENANT)")
    ap.add_argument("--skip-qbo", action="store_true", help="leave sandbox bills in place")
    ap.add_argument(
        "--sweep", action="store_true", help="also delete untracked InvoiceDesk sandbox bills"
    )
    ap.add_argument("--no-seed", action="store_true", help="skip the re-seed step")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from invoicedesk.accounting.demo_reset import DemoResetError, run_demo_reset
    from invoicedesk.db import Database

    s = settings or get_settings()
    tenant = args.tenant or s.tenant
    admin = None if (args.skip_qbo or not args.apply) else _admin(s)
    with Database(s.database_url, min_size=1, max_size=2) as db:
        try:
            report = run_demo_reset(
                db,
                tenant_id=tenant,
                apply=args.apply,
                admin=admin,  # pyright: ignore[reportArgumentType]
                skip_qbo=args.skip_qbo,
                sweep=args.sweep,
                seeder=(lambda _db, _t: "skipped") if args.no_seed else None,
            )
        except DemoResetError as exc:
            print(f"demo-reset: {exc}", file=sys.stderr)
            return 2
    for line in report.lines():
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
