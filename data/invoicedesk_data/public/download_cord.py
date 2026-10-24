"""Download the CORD v1 test split (100 receipts, naver-clova-ix/cord-v1) into the gitignored
cache and write ``index.jsonl`` with the total mapped. Images are NEVER committed.

    uv run python -m invoicedesk_data.public.download_cord [--limit N] [--cache DIR]

Uses the Hugging Face datasets-server ``/rows`` JSON API (no parquet reader needed).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from invoicedesk_data.paths import PUBLIC_CACHE_DIR
from invoicedesk_data.public.mappings import PublicRecord, map_cord

__all__ = ["DATASET", "SPLIT_SIZE", "main", "records_from_rows"]

DATASET = "naver-clova-ix/cord-v1"
SPLIT_SIZE = 100
_ROWS = "https://datasets-server.huggingface.co/rows"


def _get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "invoicedesk-data/1.0"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode("utf-8"))


def records_from_rows(payload: dict[str, Any]) -> list[tuple[PublicRecord, str]]:
    """``[(record, image_url)]`` from one ``/rows`` response."""
    out: list[tuple[PublicRecord, str]] = []
    for item in payload.get("rows", []):
        idx = int(item["row_idx"])
        row = item["row"]
        url = row.get("image", {}).get("src", "")
        rec = map_cord(idx, row["ground_truth"], f"images/{idx:03d}.jpg")
        out.append((rec, url))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="download_cord", description=__doc__)
    ap.add_argument("--cache", type=Path, default=PUBLIC_CACHE_DIR / "cord")
    ap.add_argument("--limit", type=int, default=SPLIT_SIZE)
    ap.add_argument("--labels-only", action="store_true", help="skip image download")
    args = ap.parse_args(argv)
    (args.cache / "images").mkdir(parents=True, exist_ok=True)
    rows: list[tuple[PublicRecord, str]] = []
    offset = 0
    while offset < args.limit:
        length = min(100, args.limit - offset)
        q = urllib.parse.urlencode(
            {
                "dataset": DATASET,
                "config": "default",
                "split": "test",
                "offset": offset,
                "length": length,
            }
        )
        page = records_from_rows(_get_json(f"{_ROWS}?{q}"))
        if not page:
            break
        rows.extend(page)
        offset += len(page)
    if not args.labels_only:
        for rec, url in rows:
            dest = args.cache / rec.image
            if not dest.exists() and url.startswith("https://"):
                req = urllib.request.Request(url, headers={"User-Agent": "invoicedesk-data/1.0"})  # noqa: S310
                with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                    dest.write_bytes(resp.read())
    index = args.cache / "index.jsonl"
    with index.open("w", encoding="utf-8") as fh:
        for rec, _ in rows:
            fh.write(json.dumps(rec.to_json()) + "\n")
    print(f"wrote {len(rows)} CORD records to {index}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
