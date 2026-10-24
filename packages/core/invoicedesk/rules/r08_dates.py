"""R8 date_sanity: invoice date present, not in the future, not older than ``max_age_days``;
due date (when printed) parses and is on or after the invoice date. ``today`` is injected."""

from __future__ import annotations

import datetime as dt
from typing import Any

from invoicedesk.contracts.extraction import parse_iso_date
from invoicedesk.contracts.rules import RuleContext, RuleResult
from invoicedesk.rules._common import FunctionRule, fail, passed

RULE_ID = "R8"
NAME = "date_sanity"


def check(ctx: RuleContext) -> RuleResult:
    ex = ctx.extraction
    cfg = ctx.config.dates
    today = ctx.today
    problems: list[str] = []
    fields: list[str] = []

    inv = parse_iso_date(ex.invoice_date.value_iso)
    if inv is None:
        what = "unreadable" if ex.invoice_date.value_iso else "missing"
        problems.append(f"the invoice date is {what}")
        fields.append("invoice_date")
    elif inv > today + dt.timedelta(days=cfg.allow_future_days):
        problems.append(f"the invoice date {inv.isoformat()} is in the future")
        fields.append("invoice_date")
    elif (today - inv).days > cfg.max_age_days:
        problems.append(
            f"the invoice date {inv.isoformat()} is {(today - inv).days} days old "
            f"(limit {cfg.max_age_days})"
        )
        fields.append("invoice_date")

    due_raw = ex.due_date.value_iso
    due = parse_iso_date(due_raw)
    if due_raw and due is None:
        problems.append("the due date is unreadable")
        fields.append("due_date")
    elif due is not None and inv is not None and due < inv:
        problems.append(f"the due date {due.isoformat()} is before the invoice date")
        fields.append("due_date")

    details: dict[str, Any] = {"today": today.isoformat()}
    if problems:
        msg = "Date check: " + "; ".join(problems) + "."
        return fail(RULE_ID, NAME, "review", msg, fields=fields, badge="date", **details)
    return passed(RULE_ID, NAME, "Dates look right.", **details)


RULE = FunctionRule(RULE_ID, NAME, "review", check)
