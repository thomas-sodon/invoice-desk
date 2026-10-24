"""Append-only, hash-chained audit log (CLAUDE.md invariant 5)."""

from invoicedesk.audit.chain import (
    GENESIS_HASH,
    AuditEntry,
    append,
    canonical_json,
    row_hash,
    verify,
)

__all__ = ["GENESIS_HASH", "AuditEntry", "append", "canonical_json", "row_hash", "verify"]
