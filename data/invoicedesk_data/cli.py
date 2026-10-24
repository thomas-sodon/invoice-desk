"""``invoicedesk-data``: build and check the synthetic data.

invoicedesk-data seed [--csv-only] [--database-url URL]   # data/seed/*.csv (+ Postgres)
invoicedesk-data generate [--limit N] [--augraphy]        # synthetic-core 120 (+ gold)
invoicedesk-data adversarial                              # 30 cases (+ priors, gold)
invoicedesk-data demo                                     # data/demo/ 25 files + e-mail
invoicedesk-data splits                                   # verify the frozen dev/test split
invoicedesk-data verify [SET ...]                         # rules self-check on gold
invoicedesk-data eval-examples [--database-url URL]       # gold -> eval_examples
invoicedesk-data public {sroie,cord} [--limit N]          # download public benchmarks
invoicedesk-data all                                      # seed csv + generate + adversarial + demo
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

__all__ = ["main"]


def _db(url: str | None):
    from invoicedesk.db.connection import Database

    dsn = url or os.environ.get("DATABASE_URL")
    if not dsn:
        from invoicedesk.config.settings import get_settings

        dsn = get_settings().database_url
    return Database(dsn)


def cmd_seed(a: argparse.Namespace) -> int:
    from invoicedesk_data.seed import seed_database, write_csvs

    paths = write_csvs()
    print(f"wrote {len(paths)} CSVs to data/seed/")
    if a.csv_only:
        return 0
    with _db(a.database_url) as db:
        c = seed_database(db, a.tenant)
    print(
        f"seeded tenant {a.tenant}: {c.properties} properties, {c.units} units, {c.vendors} "
        f"vendors, {c.work_orders} work orders, {c.historical_bills} historical bills "
        f"({c.inserted} new rows)"
    )
    return 0


def cmd_generate(a: argparse.Namespace) -> int:
    from invoicedesk_data.splits import build_core

    m, _ = build_core(use_augraphy=a.augraphy, limit=a.limit)
    print(f"synthetic-core: {m.count} documents -> {m.files_dir}; counts {m.counts}")
    return 0


def cmd_adversarial(a: argparse.Namespace) -> int:
    from invoicedesk_data.adversarial import build_adversarial

    m, _ = build_adversarial()
    cats = Counter(e.category for e in m.entries if e.category != "prior")
    print(f"adversarial: {sum(cats.values())} cases (+{m.count - sum(cats.values())} priors)")
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    from invoicedesk_data.demo import build_demo, expected_board

    docs, att = build_demo()
    print(f"demo: {len(docs)} files in data/demo/, board {expected_board([d.gold for d in docs])}")
    print(f"e-mail attachment: data/demo/{att.filename}")
    return 0


def cmd_splits(a: argparse.Namespace) -> int:
    from invoicedesk_data.frozen import verify_frozen

    problems = verify_frozen()
    for p in problems:
        print(f"FROZEN SPLIT PROBLEM: {p}", file=sys.stderr)
    if not problems:
        from invoicedesk_data.gold import load_manifest

        m = load_manifest("synthetic-core")
        print(f"synthetic-core frozen split OK: {m.counts}")
    return 1 if problems else 0


def cmd_verify(a: argparse.Namespace) -> int:
    from invoicedesk_data.frozen import self_check

    bad = 0
    for s in a.sets or ["synthetic-core", "adversarial", "demo"]:
        checks = self_check(s)
        failed = [c for c in checks if not c.ok]
        bad += len(failed)
        print(f"{s}: {len(checks) - len(failed)}/{len(checks)} route as labelled")
        for c in failed:
            print(f"  {c.doc_id}: expected {c.expected}, got {c.got}; {c.messages[:2]}")
    return 1 if bad else 0


def cmd_eval_examples(a: argparse.Namespace) -> int:
    from invoicedesk_data.eval_examples import load_eval_examples

    with _db(a.database_url) as db:
        n = load_eval_examples(db)
    print(f"eval_examples: {n} rows written")
    return 0


def cmd_public(a: argparse.Namespace) -> int:
    argv = ["--limit", str(a.limit)] if a.limit else []
    if a.source == "sroie":
        from invoicedesk_data.public.download_sroie import main as run
    else:
        from invoicedesk_data.public.download_cord import main as run
    return run(argv)


def cmd_all(a: argparse.Namespace) -> int:
    a.csv_only = True
    a.augraphy = False
    a.limit = None
    for fn in (cmd_seed, cmd_generate, cmd_adversarial, cmd_demo):
        rc = fn(a)
        if rc:
            return rc
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="invoicedesk-data",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("seed", help="write seed CSVs and load the universe into Postgres")
    p.add_argument("--csv-only", action="store_true")
    p.add_argument("--database-url", default=None)
    p.add_argument("--tenant", default="maple_row")
    p.set_defaults(fn=cmd_seed)
    p = sub.add_parser("generate", help="render synthetic-core (120) and its gold")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--augraphy", action="store_true", help="use Augraphy if installed")
    p.set_defaults(fn=cmd_generate)
    p = sub.add_parser("adversarial", help="render the 30-case adversarial set")
    p.set_defaults(fn=cmd_adversarial)
    p = sub.add_parser("demo", help="render the 25-file demo batch into data/demo/")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("splits", help="verify the frozen synthetic-core dev/test split")
    p.set_defaults(fn=cmd_splits)
    p = sub.add_parser("verify", help="route gold through the rules engine (label QA)")
    p.add_argument("sets", nargs="*")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("eval-examples", help="load gold into eval_examples")
    p.add_argument("--database-url", default=None)
    p.set_defaults(fn=cmd_eval_examples)
    p = sub.add_parser("public", help="download SROIE or CORD into data/public/cache")
    p.add_argument("source", choices=["sroie", "cord"])
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(fn=cmd_public)
    p = sub.add_parser("all", help="seed CSVs, synthetic-core, adversarial and demo")
    p.add_argument("--tenant", default="maple_row")
    p.add_argument("--database-url", default=None)
    p.set_defaults(fn=cmd_all)
    a = ap.parse_args(argv)
    return int(a.fn(a))


if __name__ == "__main__":
    raise SystemExit(main())
