"""Try-it consumption limits (M14; DESIGN §5.5 Ops row, §7.3 "$3/day hard cap").

* Request counters (``tryit_counters``, migration 0400): uploads per session, uploads and
  sessions per IP per day, anonymous demo runs per IP per day. ``take()`` increments only while
  under the limit, atomically, so concurrent requests cannot overshoot.
* Dollars: the pipeline adds every visitor document's LLM cost to ``spend_ledger`` (scope
  ``global``, key ``tryit``, and per session) only as each call finishes. The cap is *hard*
  (DESIGN §7.3): it counts as reached once the day's recorded spend plus a one-document reserve
  for every visitor document still in flight, and for the one about to be accepted, would pass
  ``PUBLIC_DAILY_SPEND_CAP_USD``. Without the in-flight reserve, a burst of concurrent uploads
  would all pass the check before any of their cost is recorded. The try-it page then
  switches to the recorded replay batch.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from invoicedesk.db import repo
from invoicedesk.db.connection import Conn

from invoicedesk_api.runtime import Runtime

__all__ = [
    "GLOBAL_SPEND_KEY",
    "Scope",
    "count",
    "in_flight_visitor_docs",
    "spend_cap_reached",
    "take",
    "today",
]

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


#: A visitor document stuck in flight longer than this no longer holds a reserve (its job
#: failed or was abandoned; the stale-job sweep requeues it).
IN_FLIGHT_WINDOW = dt.timedelta(hours=1)


def in_flight_visitor_docs(conn: Conn, rt: Runtime) -> int:
    from invoicedesk.pipeline.events import IN_FLIGHT

    row = conn.execute(
        "select count(*) as n from documents where tenant_id = %s and session_id is not null"
        " and status::text = any(%s) and created_at >= %s",
        (rt.tenant, sorted(IN_FLIGHT), rt.clock() - IN_FLIGHT_WINDOW),
    ).fetchone()
    return 0 if row is None else int(row["n"])


def spend_cap_reached(conn: Conn, rt: Runtime) -> bool:
    spent = repo.get_spend(conn, scope="global", key=GLOBAL_SPEND_KEY, day=today(rt))
    cap = Decimal(rt.settings.public_daily_spend_cap_usd)
    reserve = Decimal(rt.api.tryit_cost_reserve_usd) * (in_flight_visitor_docs(conn, rt) + 1)
    return spent + reserve > cap
