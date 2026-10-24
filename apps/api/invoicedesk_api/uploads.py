"""Upload allow-list and document creation (M1, M2, M14).

The gateway enforces the input limits *before* anything is stored: JPEG, PNG, HEIC/HEIF or PDF
by content (magic bytes, never the filename or the client's header), at most 10 MB and at most
5 pages. The preprocessor checks again and sanitizes; this is the cheap first gate.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from fastapi import UploadFile
from invoicedesk.contracts.infra import BoardEvent
from invoicedesk.contracts.preprocess import ALLOWED_MIME, MAX_BYTES, MAX_PAGES, RejectedInput
from invoicedesk.db import repo
from invoicedesk.preprocess.images import sniff_mime
from invoicedesk.status import Actor, kanban_column

from invoicedesk_api.runtime import Runtime

__all__ = [
    "CheckedFile",
    "check_upload",
    "create_document",
    "publish_created",
    "read_limited",
    "safe_filename",
]

log = logging.getLogger("invoicedesk.api.uploads")

_GENERIC = {"", "application/octet-stream", "binary/octet-stream"}
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")
VISITOR_TTL = dt.timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class CheckedFile:
    filename: str
    mime: str
    pages: int
    data: bytes
    sha256: str


def safe_filename(name: str | None, default: str = "invoice") -> str:
    """Basename only, ASCII-safe, at most 120 characters (it is shown on the board)."""
    raw = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    raw = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    raw = _UNSAFE.sub("_", raw).strip(" ._") or default
    if len(raw) > 120:
        stem, dot, ext = raw.rpartition(".")
        raw = (stem[: 115 - len(ext)] + dot + ext) if dot and len(ext) <= 5 else raw[:120]
    return raw


def _pdf_pages(data: bytes) -> int:
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise RejectedInput("The PDF could not be read.") from exc
    try:
        if doc.needs_pass and not doc.authenticate(""):
            raise RejectedInput("Password-protected PDFs are not supported.")
        return int(doc.page_count)
    finally:
        doc.close()


def check_upload(data: bytes, *, filename: str | None, declared: str | None) -> CheckedFile:
    """Raise :class:`RejectedInput` (shown to the user) unless the file is allowed."""
    if not data:
        raise RejectedInput("The file is empty.")
    if len(data) > MAX_BYTES:
        raise RejectedInput(f"The file is over the {MAX_BYTES // 1_048_576} MB limit.")
    decl = (declared or "").split(";")[0].strip().lower()
    if decl not in ALLOWED_MIME and decl not in _GENERIC:
        raise RejectedInput("Only PDF, JPEG, PNG and HEIC files are accepted.")
    mime = sniff_mime(data)
    if mime is None or mime not in ALLOWED_MIME:
        raise RejectedInput("Only PDF, JPEG, PNG and HEIC files are accepted.")
    pages = _pdf_pages(data) if mime == "application/pdf" else 1
    if pages < 1:
        raise RejectedInput("The PDF has no pages.")
    if pages > MAX_PAGES:
        raise RejectedInput(f"The PDF has {pages} pages; the limit is {MAX_PAGES}.")
    return CheckedFile(
        filename=safe_filename(filename),
        mime=mime,
        pages=pages,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
    )


async def read_limited(upload: UploadFile) -> bytes:
    """Read an ``UploadFile`` but stop one byte past the limit (no unbounded buffering)."""
    return bytes(await upload.read(MAX_BYTES + 1))


def create_document(
    rt: Runtime,
    f: CheckedFile,
    *,
    source: Literal["upload", "email", "seed", "mcp"],
    actor: Actor,
    batch_id: str | None = None,
    uploaded_by: str | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
) -> str:
    """Insert the ``documents`` row (+ audit), store the original, link an e-mail thread.

    One transaction: if storing the bytes fails, no row survives; if the commit fails, the
    stored object is removed again."""
    prefix = f"visitor/{session_id}" if session_id else rt.tenant
    key: str | None = None
    try:
        with rt.db.tx() as conn:
            doc_id = repo.insert_document(
                conn,
                tenant_id=rt.tenant,
                source=source,
                sha256=f.sha256,
                storage_path="pending",
                mime=f.mime,
                pages=f.pages,
                filename=f.filename,
                batch_id=batch_id,
                session_id=session_id,
                uploaded_by=uploaded_by,
                purge_after=rt.clock() + VISITOR_TTL if session_id else None,
                actor=actor,
            )
            key = f"{prefix}/originals/{doc_id}/{f.filename}"
            rt.storage.put(key, f.data, content_type=f.mime)
            conn.execute("update documents set storage_path = %s where id = %s", (key, doc_id))
            if thread_id is not None:
                conn.execute(
                    "insert into document_threads (document_id, thread_id) values (%s, %s)",
                    (doc_id, thread_id),
                )
    except Exception:
        if key is not None:
            try:
                rt.storage.delete(key)
            except Exception:
                log.warning("could not remove orphaned object after a failed insert")
        raise
    return doc_id


def publish_created(rt: Runtime, document_id: str, batch_id: str | None) -> None:
    try:
        rt.publisher.publish(
            BoardEvent(
                type="document.created",
                tenant_id=rt.tenant,
                document_id=document_id,
                batch_id=batch_id,
                status="Received",
                column=kanban_column("Received"),
                cost_usd="0",
            )
        )
    except Exception:  # realtime is best effort; the DB is the source of truth
        log.warning("document.created publish failed", exc_info=True)
