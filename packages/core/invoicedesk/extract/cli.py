"""``invoicedesk extract FILE``: preprocess, screen, tiered-extract, print JSON with word refs.

FILE is an upload (PDF/JPEG/PNG/HEIC) or a ``PreprocessedDocument`` JSON (then page images are
read from ``--storage-dir``). Screening always runs first (invariant 8): the guardrails
component's screeners, or a ``ScreeningOutcome`` JSON passed with ``--screening``. Quarantined
documents are never extracted (exit 4).

Offline (the default, ``INVOICEDESK_MODE=offline``) answers come from recordings in
``LLM_REPLAY_DIR`` and otherwise from a deterministic OCR-heuristic stand-in
(:mod:`invoicedesk.extract.offline`); no network call is ever made. ``--live`` (or
``INVOICEDESK_MODE=live``) uses the real Claude client and costs money.

Output is passed through :func:`invoicedesk.masking.mask_payload` (bank numbers -> last 4).
Exit codes: 0 ok, 1 rejected input, 3 a needed component is unavailable, 4 quarantined,
5 no tier produced a valid extraction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from invoicedesk.calibrate import load_calibrator
from invoicedesk.config.settings import get_settings
from invoicedesk.config.tenant import ConfidenceConfig, load_rules_config
from invoicedesk.contracts.extraction import MONEY_FIELDS
from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.llm import LLMClient
from invoicedesk.contracts.preprocess import PreprocessedDocument, RejectedInput
from invoicedesk.contracts.screening import ScreeningOutcome
from invoicedesk.extract import ExtractionFailed, QuarantinedDocument, build_extractor
from invoicedesk.extract.offline import offline_llm
from invoicedesk.fakes.llm import ReplayLLM
from invoicedesk.masking import mask_payload
from invoicedesk.storage import InMemoryStorage, LocalStorage

__all__ = ["main"]


def _is_money(name: str) -> bool:
    return name in MONEY_FIELDS or name.endswith((".unit_price", ".amount"))


def _err(payload: dict[str, Any], code: int) -> int:
    print(json.dumps(mask_payload(payload), indent=2), file=sys.stderr)
    return code


def _llm(live: bool, replay_dir: Path | None) -> LLMClient:
    settings = get_settings()
    if live or settings.mode == "live":
        from invoicedesk.wiring import llm_client

        return llm_client(settings.model_copy(update={"mode": "live"}))
    return ReplayLLM(replay_dir or settings.replay_dir, fallback=offline_llm())


def _confidence() -> ConfidenceConfig:
    s = get_settings()
    try:
        return load_rules_config(s.tenant, s.tenants_dir).confidence
    except (FileNotFoundError, ValueError):
        return ConfidenceConfig()


def _preprocess(path: Path, storage: Storage) -> PreprocessedDocument:
    data = path.read_bytes()
    if path.suffix.lower() == ".json":
        return PreprocessedDocument.model_validate_json(data)
    from invoicedesk.preprocess import build_preprocessor
    from invoicedesk.preprocess.images import sniff_mime

    mime = sniff_mime(data) or "application/octet-stream"
    doc_id = hashlib.sha256(data).hexdigest()[:16]
    return build_preprocessor(storage, key_prefix="cli").preprocess(
        data, mime=mime, document_id=doc_id, filename=path.name
    )


def _screen(
    doc: PreprocessedDocument, llm: LLMClient, email_body: str | None, screening: Path | None
) -> ScreeningOutcome:
    if screening is not None:
        return ScreeningOutcome.model_validate_json(screening.read_text(encoding="utf-8"))
    from invoicedesk.guardrails import build_screeners, run_screening

    return run_screening(doc, build_screeners(llm), email_body=email_body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="invoicedesk extract", description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--email-body", type=Path, help="text file with the email body (untrusted)")
    ap.add_argument("--screening", type=Path, help="ScreeningOutcome JSON (skip live screening)")
    ap.add_argument("--storage-dir", type=Path, help="LocalStorage root for page images")
    ap.add_argument("--max-tier", choices=["haiku", "sonnet", "opus"], default="opus")
    ap.add_argument("--calibrator", type=Path, help="fitted calibrator JSON artifact")
    ap.add_argument("--replay-dir", type=Path, help="recordings directory (offline mode)")
    ap.add_argument("--live", action="store_true", help="call the real API (costs money)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    storage: Storage = (
        LocalStorage(args.storage_dir, signing_secret="cli-local")  # noqa: S106 - local only
        if args.storage_dir
        else InMemoryStorage()
    )
    email_body = args.email_body.read_text(encoding="utf-8") if args.email_body else None
    try:
        llm = _llm(args.live, args.replay_dir)
    except RuntimeError as exc:
        return _err({"error": str(exc)}, 3)

    try:
        doc = _preprocess(args.file, storage)
    except RejectedInput as exc:
        print(json.dumps({"rejected": str(exc)}))
        return 1
    except (NotImplementedError, ImportError) as exc:
        return _err({"error": f"preprocessor unavailable: {exc}"}, 3)

    try:
        screening = _screen(doc, llm, email_body, args.screening)
    except (NotImplementedError, ImportError) as exc:
        return _err(
            {
                "error": "screening is unavailable, and extraction never runs unscreened "
                f"(pass --screening with a ScreeningOutcome JSON): {exc}"
            },
            3,
        )

    calibrator = load_calibrator(args.calibrator)
    extractor = build_extractor(
        llm,
        calibrator=calibrator,
        storage=storage,
        confidence=_confidence(),
    )
    try:
        outcome = extractor.extract(
            doc, screening=screening, email_body=email_body, max_tier=args.max_tier
        )
    except QuarantinedDocument:
        print(
            json.dumps(
                mask_payload(
                    {
                        "document_id": doc.document_id,
                        "quarantined": True,
                        "screening": screening.model_dump(mode="json"),
                    }
                ),
                indent=2,
            )
        )
        return 4
    except ExtractionFailed as exc:
        return _err(
            {
                "document_id": doc.document_id,
                "error": str(exc),
                "tiers": [t.model_dump(mode="json") for t in exc.tiers],
            },
            5,
        )

    payload: dict[str, Any] = {
        "document_id": doc.document_id,
        "quarantined": False,
        "calibrator": calibrator.version,
        **outcome.model_dump(mode="json"),
    }
    # Money values print as integer cents (ints are not account numbers, so masking keeps
    # them); every string, including remit-to text, is still masked to the last 4.
    for row in payload["fields"]:
        if _is_money(row["name"]) and row["value"] is not None:
            row["value"] = int(row["value"])
    masked = mask_payload(payload)
    # API costs are decimal strings such as "0.00012345"; the digit-run masker would mangle
    # them, and they are never account numbers, so restore them after masking.
    masked["total_cost_usd"] = outcome.total_cost_usd
    for row, tier in zip(masked["tiers"], outcome.tiers, strict=True):
        row["cost_usd"] = tier.cost_usd
    print(json.dumps(masked, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
