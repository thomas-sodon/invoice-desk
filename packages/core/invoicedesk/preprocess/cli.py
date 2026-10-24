"""``invoicedesk preprocess FILE``: run the preprocessor locally and print JSON.

Renders go to a throwaway in-memory store unless ``--storage-dir`` is given. Text is passed
through :func:`invoicedesk.masking.mask_payload`, so long digit runs (account numbers) print as
last-4 only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.preprocess import OcrEngine, RejectedInput
from invoicedesk.masking import mask_payload
from invoicedesk.preprocess.images import sniff_mime
from invoicedesk.preprocess.ocr import DocTREngine, EngineUnavailable
from invoicedesk.preprocess.pipeline import DefaultPreprocessor
from invoicedesk.storage import InMemoryStorage, LocalStorage


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="invoicedesk preprocess", description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--mime", help="declared content type (default: sniffed from content)")
    ap.add_argument("--document-id", help="default: first 16 hex of the file's sha256")
    ap.add_argument("--prefix", default="cli", help="storage key prefix (tenant or visitor/...)")
    ap.add_argument("--storage-dir", type=Path, help="write page PNGs here (LocalStorage)")
    ap.add_argument("--engine", choices=["rapidocr", "doctr"], default="rapidocr")
    ap.add_argument("--no-enhanced", action="store_true", help="skip the CLAHE OCR pass")
    ap.add_argument("--summary", action="store_true", help="print counts and timing only")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    data = args.file.read_bytes()
    storage: Storage = (
        LocalStorage(args.storage_dir, signing_secret="cli-local")  # noqa: S106 - local only
        if args.storage_dir
        else InMemoryStorage()
    )
    ocr: OcrEngine | None = None
    if args.engine == "doctr":
        ocr = DocTREngine()
    pre = DefaultPreprocessor(
        storage, ocr=ocr, key_prefix=args.prefix, enhanced_pass=not args.no_enhanced
    )
    doc_id = args.document_id or hashlib.sha256(data).hexdigest()[:16]
    mime = args.mime or sniff_mime(data) or "application/octet-stream"
    t0 = time.perf_counter()
    try:
        out = pre.preprocess(data, mime=mime, document_id=doc_id, filename=args.file.name)
    except RejectedInput as exc:
        print(json.dumps({"rejected": str(exc)}))
        return 1
    except EngineUnavailable as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    elapsed = time.perf_counter() - t0
    if args.summary:
        payload = {
            "document_id": out.document_id,
            "mime": out.mime,
            "is_native_pdf": out.is_native_pdf,
            "pages": len(out.pages),
            "words": len(out.words),
            "enhanced_words": len(out.enhanced_words),
            "text_layer": len(out.text_layer),
            "phash": out.phash,
            "sanitize": out.sanitize.model_dump(),
            "seconds": round(elapsed, 3),
        }
    else:
        payload = out.model_dump(mode="json")
        payload["seconds"] = round(elapsed, 3)
    print(json.dumps(mask_payload(payload), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
