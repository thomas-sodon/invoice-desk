"""Download the SROIE Task 3 mirror (Voxel51/scanned_receipts) into the gitignored cache and
write ``index.jsonl`` with vendor/date/total mapped. Images are NEVER committed.

    uv run python -m invoicedesk_data.public.download_sroie [--limit N] [--cache DIR]

Network access: Hugging Face only. The mirror holds 712 receipts with company, date, address
and total labels (the ICDAR 2019 SROIE Task 3 fields).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from invoicedesk_data.paths import PUBLIC_CACHE_DIR
from invoicedesk_data.public.mappings import PublicRecord, map_sroie

__all__ = ["REPO_ID", "build_index", "main"]

REPO_ID = "Voxel51/scanned_receipts"


def build_index(samples_json: Path, *, limit: int | None = None) -> list[PublicRecord]:
    data = json.loads(samples_json.read_text(encoding="utf-8"))
    samples = data.get("samples", data if isinstance(data, list) else [])
    out = [map_sroie(s) for s in samples]
    return out[:limit] if limit else out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="download_sroie", description=__doc__)
    ap.add_argument("--cache", type=Path, default=PUBLIC_CACHE_DIR / "sroie")
    ap.add_argument("--limit", type=int, default=None, help="only index the first N receipts")
    ap.add_argument("--labels-only", action="store_true", help="skip image download")
    args = ap.parse_args(argv)

    from huggingface_hub import hf_hub_download, snapshot_download

    args.cache.mkdir(parents=True, exist_ok=True)
    samples = Path(
        hf_hub_download(REPO_ID, "samples.json", repo_type="dataset", local_dir=args.cache)
    )
    records = build_index(samples, limit=args.limit)
    if not args.labels_only:
        patterns = [r.image for r in records] if args.limit else ["data/*"]
        snapshot_download(
            REPO_ID, repo_type="dataset", local_dir=args.cache, allow_patterns=patterns
        )
    index = args.cache / "index.jsonl"
    with index.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r.to_json()) + "\n")
    print(f"wrote {len(records)} SROIE records to {index}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
