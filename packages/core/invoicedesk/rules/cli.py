"""``invoicedesk validate``: run R1-R10 on an extraction JSON against tenant-data JSON.

Example::

    invoicedesk validate extraction.json tenant_data.json --today 2026-10-24 --raw-conf

``extraction.json`` is an :class:`InvoiceExtraction`; ``tenant_data.json`` is a
:class:`TenantData`. Optional JSON inputs: ``--conf`` (``{field: calibrated_prob}``),
``--screening`` (:class:`ScreeningOutcome`), ``--document`` (:class:`DocumentInfo`) and
``--approvals`` (a list of :class:`ApprovalRecord`). Without ``--conf``, R9 holds every
required field (no calibrated confidence) unless ``--raw-conf`` uses the model's self-reports,
which is for local debugging only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from invoicedesk.config.tenant import RulesConfig, load_rules_config, parse_rules_yaml
from invoicedesk.contracts.extraction import InvoiceExtraction, iter_header_fields
from invoicedesk.contracts.rules import DocumentInfo, RuleContext
from invoicedesk.contracts.screening import ScreeningOutcome
from invoicedesk.contracts.tenant_data import ApprovalRecord, TenantData
from invoicedesk.rules import build_rules_engine
from invoicedesk.rules.engine import route_results

__all__ = ["main"]


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="invoicedesk validate", description="Run rules R1-R10 on an extraction."
    )
    p.add_argument("extraction", help="InvoiceExtraction JSON file")
    p.add_argument("tenant_data", help="TenantData JSON file")
    p.add_argument("--tenant", default="maple_row", help="tenant id (default maple_row)")
    p.add_argument("--tenants-dir", type=Path, default=None, help="directory holding tenants")
    p.add_argument("--config", type=Path, default=None, help="explicit rules.yaml path")
    p.add_argument("--today", type=dt.date.fromisoformat, default=None, help="YYYY-MM-DD")
    p.add_argument("--conf", default=None, help="calibrated confidences JSON {field: prob}")
    p.add_argument("--raw-conf", action="store_true", help="use raw confidences (debug only)")
    p.add_argument("--screening", default=None, help="ScreeningOutcome JSON")
    p.add_argument("--document", default=None, help="DocumentInfo JSON")
    p.add_argument("--approvals", default=None, help="list of ApprovalRecord JSON")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    return p


def _config(args: argparse.Namespace) -> RulesConfig:
    if args.config is not None:
        cfg = parse_rules_yaml(args.config.read_text(encoding="utf-8"))
        return cfg
    return load_rules_config(args.tenant, args.tenants_dir)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    ex = InvoiceExtraction.model_validate(_read(args.extraction))
    tenant = TenantData.model_validate(_read(args.tenant_data))
    if args.conf is not None:
        conf = {str(k): float(v) for k, v in _read(args.conf).items()}
    elif args.raw_conf:
        conf = {name: f.confidence for name, f in iter_header_fields(ex)}
    else:
        conf = {}
    screening = (
        ScreeningOutcome.model_validate(_read(args.screening))
        if args.screening
        else ScreeningOutcome(results=[], quarantined=False)
    )
    document = (
        DocumentInfo.model_validate(_read(args.document))
        if args.document
        else DocumentInfo(document_id="cli", sha256="")
    )
    approvals = (
        tuple(TypeAdapter(list[ApprovalRecord]).validate_python(_read(args.approvals)))
        if args.approvals
        else ()
    )
    today = args.today or dt.datetime.now(dt.UTC).date()
    ctx = RuleContext(
        extraction=ex,
        calibrated_conf=conf,
        screening=screening,
        tenant=tenant,
        config=_config(args),
        document=document,
        today=today,
        approvals=approvals,
    )
    results = build_rules_engine().evaluate(ctx)
    decision = route_results(results)
    if args.json:
        payload = {
            "route": decision.target.value,
            "results": [r.model_dump(mode="json") for r in results],
        }
        print(json.dumps(payload, indent=2))
    else:
        for r in results:
            print(f"{r.rule_id:<4} {r.name:<18} {r.severity.upper():<7} {r.message}")
        print(f"\nRoute: {decision.target.value}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
