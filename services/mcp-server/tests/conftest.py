"""MCP server test fixtures (helpers live in ``invoicedesk_mcp.testkit``)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from invoicedesk.db.connection import Database
from invoicedesk_api.testkit import Env, build_env
from invoicedesk_mcp.testkit import Keys, make_keys


@pytest.fixture(scope="session")
def keys() -> Keys:
    return make_keys()


@pytest.fixture
def env(pg_db: Database, tmp_path: Path) -> Iterator[Env]:
    e = build_env(pg_db, tmp_path / "storage")
    try:
        yield e
    finally:
        e.client.close()
        e.rt.close()
