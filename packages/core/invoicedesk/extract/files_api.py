"""Upload each processed page image once (Files API) and reuse its ``file_id`` across tiers.

The cache is keyed by the page PNG's SHA-256, so the three tiers of one document, and a
re-processed identical page, all share one upload. Bounded LRU so a long-running worker does
not grow without limit.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.llm import FileImageBlock, LLMClient
from invoicedesk.contracts.preprocess import PreprocessedDocument

__all__ = ["PageImageUploader"]


class PageImageUploader:
    def __init__(self, llm: LLMClient, storage: Storage, *, max_entries: int = 4096) -> None:
        self._llm = llm
        self._storage = storage
        self._max = max_entries
        self._ids: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self.uploads = 0

    def file_id(self, sha256: str, storage_key: str, *, filename: str) -> str:
        with self._lock:
            if sha256 in self._ids:
                self._ids.move_to_end(sha256)
                return self._ids[sha256]
            data = self._storage.get(storage_key)
            fid = self._llm.upload_file(data, filename=filename, media_type="image/png")
            self.uploads += 1
            self._ids[sha256] = fid
            while len(self._ids) > self._max:
                self._ids.popitem(last=False)
            return fid

    def image_blocks(self, doc: PreprocessedDocument) -> tuple[FileImageBlock, ...]:
        """One image block per page, in page order. Filenames carry no document ID."""
        blocks: list[FileImageBlock] = []
        for page in sorted(doc.pages, key=lambda p: p.index):
            fid = self.file_id(
                page.sha256, page.storage_key, filename=f"page-{page.sha256[:16]}.png"
            )
            blocks.append(FileImageBlock(file_id=fid))
        return tuple(blocks)
