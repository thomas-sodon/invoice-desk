"""API test fixtures (``invoicedesk_api.testkit`` builds the environment)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from invoicedesk.db.connection import Database
from invoicedesk.testing.postgres import TEMPLATE_DB, PgServer
from invoicedesk_api.testkit import Env, build_env
from psycopg import sql


@pytest.fixture
def env_factory(pg_db: Database, tmp_path: Path) -> Iterator[Callable[..., Env]]:
    made: list[Env] = []

    def factory(**kw: Any) -> Env:
        e = build_env(pg_db, tmp_path / f"storage{len(made)}", **kw)
        made.append(e)
        return e

    yield factory
    for e in made:
        e.client.close()
        e.rt.close()


@pytest.fixture
def env(env_factory: Callable[..., Env]) -> Env:
    return env_factory()


@pytest.fixture(scope="module")
def module_db(pg_server: PgServer) -> Iterator[Database]:
    """One migrated database for a whole test module (the 25-document demo runs once)."""
    name = "m_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg_server.admin_dsn, autocommit=True) as c:
        c.execute(
            sql.SQL("create database {} template {}").format(
                sql.Identifier(name), sql.Identifier(TEMPLATE_DB)
            )
        )
    db = Database(pg_server.dsn_for(name), min_size=1, max_size=6)
    try:
        yield db
    finally:
        db.close()
        with psycopg.connect(pg_server.admin_dsn, autocommit=True) as c:
            c.execute(
                sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name))
            )
