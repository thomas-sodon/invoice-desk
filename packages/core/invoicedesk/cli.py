"""``invoicedesk`` CLI. Subcommands are dispatched lazily to the owning component's module so
components can add commands without editing this file (each target is ``module:main(argv)``)."""

from __future__ import annotations

import importlib
import sys

COMMANDS: dict[str, tuple[str, str]] = {
    "migrate": ("invoicedesk.db.migrate:main", "apply SQL migrations"),
    "audit-verify": ("invoicedesk.audit.cli:main", "verify the audit-log hash chain"),
    "preprocess": ("invoicedesk.preprocess.cli:main", "preprocess a file, print JSON"),
    "screen": ("invoicedesk.guardrails.cli:main", "screen a file, print verdicts"),
    "extract": ("invoicedesk.extract.cli:main", "extract a file, print JSON with word refs"),
    "validate": ("invoicedesk.rules.cli:main", "run rules on an extraction JSON"),
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help") or args[0] not in COMMANDS:
        print("usage: invoicedesk <command> [args]\n")
        for name, (_, help_) in COMMANDS.items():
            print(f"  {name:<14} {help_}")
        return 0 if args and args[0] in ("-h", "--help") else 2
    target, _ = COMMANDS[args[0]]
    mod, _, fn = target.partition(":")
    sys.argv = [f"invoicedesk {args[0]}", *args[1:]]
    try:
        func = getattr(importlib.import_module(mod), fn)
    except ModuleNotFoundError as exc:
        print(f"command {args[0]!r} is not available yet ({exc.name})", file=sys.stderr)
        return 3
    result = func()
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
