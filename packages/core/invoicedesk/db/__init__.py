"""Thin Postgres access layer (psycopg 3, plain SQL, dict rows)."""

from invoicedesk.db.connection import Database
from invoicedesk.db.migrate import MIGRATIONS_DIR, apply_migrations

__all__ = ["MIGRATIONS_DIR", "Database", "apply_migrations"]
