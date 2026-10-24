"""``invoicedesk screen FILE``: preprocess and screen one file, print the verdicts as JSON.

FILE is an upload (PDF/JPEG/PNG/HEIC) or a ``PreprocessedDocument`` JSON (page renders are then
read from ``--storage-dir`` when given). ``--email-body`` adds the untrusted email body.

Offline (the default) the classifier answers from recordings in ``LLM_REPLAY_DIR`` and otherwise
from the deterministic offline stand-in (FakeLLM); no network call is made. ``--live`` (or
``INVOICEDESK_MODE=live``) uses the real Claude client and costs money.

Output spans are masked (bank/account numbers to the last 4).
Exit codes: 0 screened and not quarantined, 4 quarantined, 1 rejected input, 3 a needed
component is unavailable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from invoicedesk.config.settings import get_settings
from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.llm import LLMCallError, LLMClient
from invoicedesk.contracts.preprocess import PreprocessedDocument, RejectedInput
from invoicedesk.guardrails import build_screeners, run_screening
from invoicedesk.masking import mask_payload
from invoicedesk.storage import InMemoryStorage, LocalStorage

__all__ = ["main"]


def _llm(live: bool, replay_dir: Path | None) -> LLMClient:
    settings = get_settings()
    if live or settings.mode == "live":
        from invoicedesk.wiring import llm_client

        return llm_client(settings.model_copy(update={"mode": "live"}))
    from invoicedesk.extract.offline import offline_llm
    from invoicedesk.fakes.llm import ReplayLLM

    return ReplayLLM(replay_dir or settings.replay_dir, fallback=offline_llm())


def _load(path: Path, storage: Storage) -> PreprocessedDocument:
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="invoicedesk screen", description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--email-body", type=Path, help="text file with the email body (untrusted)")
    ap.add_argument("--storage-dir", type=Path, help="LocalStorage root for page renders")
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
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    try:
        doc = _load(args.file, storage)
    except RejectedInput as exc:
        print(json.dumps({"rejected": str(exc)}))
        return 1
    try:
        outcome = run_screening(doc, build_screeners(llm, storage=storage), email_body=email_body)
    except LLMCallError as exc:
        print(json.dumps({"error": f"classifier unavailable: {exc.kind}"}), file=sys.stderr)
        return 3

    payload = {
        "document_id": doc.document_id,
        "quarantined": outcome.quarantined,
        "categories": sorted(outcome.categories),
        "results": [r.model_dump(mode="json") for r in outcome.results],
    }
    print(json.dumps(mask_payload(payload), indent=2, ensure_ascii=False))
    return 4 if outcome.quarantined else 0


if __name__ == "__main__":
    raise SystemExit(main())
