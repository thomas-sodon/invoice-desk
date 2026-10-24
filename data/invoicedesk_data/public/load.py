"""Load the downloaded public sets (for the evals component). Returns ``[]`` if not downloaded."""

from __future__ import annotations

import json
from pathlib import Path

from invoicedesk_data.paths import PUBLIC_CACHE_DIR
from invoicedesk_data.public.mappings import PublicRecord

__all__ = ["load_public"]


def load_public(source: str, cache: Path = PUBLIC_CACHE_DIR) -> list[PublicRecord]:
    index = cache / source / "index.jsonl"
    if not index.exists():
        return []
    return [
        PublicRecord(**json.loads(line))
        for line in index.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
