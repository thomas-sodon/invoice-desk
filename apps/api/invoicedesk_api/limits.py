"""Try-it consumption limits (M14; DESIGN §5.5 Ops row, §7.3 "$3/day hard cap").

* Request counters (``tryit_counters``, migration 0400): uploads per session, uploads and
  sessions per IP per day, anonymous demo runs per IP per day. ``take()`` increments only while
  under the limit, atomically, so concurrent requests cannot overshoot.
* Dollars: the pipeline adds every visitor document's LLM cost to ``spend_ledger`` (scope
  ``global``, key ``tryit``, and per session). The cap counts as reached once the day's spend
  plus a one-document reserve would pass ``PUBLIC_DAILY_SPEND_CAP_USD``; the try-it page then
  switches to the recorded replay batch.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from invoicedesk.db import repo
from invoicedesk.db.connection import Conn

from invoicedesk_api.runtime import Runtime

__all__ = ["GLOBAL_SPEND_KEY", "Scope", "count", "spend_cap_reached", "take", "today"]

GLOBAL_SPEND_KEY = "tryit"
Scope = Literal["ip_upload", "ip_session", "session_upload", "ip_demo"]


def today(rt: Runtime) -> dt.date:
    return rt.clock().astimezone(dt.UTC).date()


def take(conn: Conn, *, day: dt.date, scope: Scope, key: str, limit: int) -> bool:
    """Consume one unit if the counter is below ``limit``; False when the limit is reached."""
    if limit <= 0:
        return False
    row = conn.execute(
        "insert into tryit_counters (day, scope, key, n) values (%s, %s, %s, 1)"
        " on conflict (day, scope, key) do update set n = tryit_counters.n + 1"
        " where tryit_counters.n < %s returning n",
        (day, scope, key, limit),
    ).fetchone()
    return row is not None


def count(conn: Conn, *, day: dt.date, scope: Scope, key: str) -> int:
    row = conn.execute(
        "select n from tryit_counters where day = %s and scope = %s and key = %s",
        (day, scope, key),
    ).fetchone()
    return 0 if row is None else int(row["n"])


def spend_cap_reached(conn: Conn, rt: Runtime) -> bool:
    spent = repo.get_spend(conn, scope="global", key=GLOBAL_SPEND_KEY, day=today(rt))
    cap = Decimal(rt.settings.public_daily_spend_cap_usd)
    return spent + Decimal(rt.api.tryit_cost_reserve_usd) > cap
