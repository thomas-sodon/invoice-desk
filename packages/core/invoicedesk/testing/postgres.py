"""Throwaway Postgres cluster for tests.

Usage (already registered by the repo-root conftest.py)::

    @pytest.mark.postgres
    def test_x(pg_db: Database): ...

* ``INVOICEDESK_TEST_DSN`` set -> use that server (CI service container); each test still gets a
  fresh database cloned from a migrated template.
* otherwise -> ``initdb`` a cluster under a temp dir (run as the ``postgres`` OS user when the
  tests run as root) on a free localhost port.
* neither possible -> tests using the fixtures are skipped cleanly.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from invoicedesk.db.connection import Database
from invoicedesk.db.migrate import apply_migrations

__all__ = ["PgServer", "find_pg_bin", "pg_db", "pg_dsn", "pg_server"]

TEMPLATE_DB = "invoicedesk_template"


def find_pg_bin() -> Path | None:
    for cand in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if Path(cand, "initdb").exists() and Path(cand, "pg_ctl").exists():
            return Path(cand)
    found = shutil.which("initdb")
    return Path(found).parent if found else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class PgServer:
    admin_dsn: str  # connects to the 'postgres' maintenance db
    _base: str  # dsn without dbname
    _stop: list[str] | None = None
    _datadir: str | None = None
    _as_user: list[str] | None = None

    def dsn_for(self, dbname: str) -> str:
        return f"{self._base} dbname={dbname}"

    def stop(self) -> None:
        if self._stop:
            subprocess.run(self._stop, check=False, capture_output=True)
        if self._datadir:
            shutil.rmtree(self._datadir, ignore_errors=True)


def _start_local() -> PgServer:
    bindir = find_pg_bin()
    if bindir is None:
        pytest.skip("Postgres binaries not found (initdb/pg_ctl)")
    tmp = tempfile.mkdtemp(prefix="invoicedesk-pg-")
    data = os.path.join(tmp, "data")
    as_user: list[str] = []
    if os.geteuid() == 0:
        if shutil.which("runuser") is None:
            pytest.skip("running as root without runuser; cannot start Postgres")
        shutil.chown(tmp, user="postgres")
        os.chmod(tmp, 0o700)
        as_user = ["runuser", "-u", "postgres", "--"]
    port = _free_port()
    subprocess.run(
        [
            *as_user,
            str(bindir / "initdb"),
            "-D",
            data,
            "-A",
            "trust",
            "-U",
            "postgres",
            "--no-sync",
            "-E",
            "UTF8",
            "--locale=C",
        ],
        check=True,
        capture_output=True,
    )
    log = os.path.join(tmp, "pg.log")
    opts = f"-p {port} -k {tmp} -c listen_addresses=127.0.0.1 -c fsync=off -c full_page_writes=off"
    res = subprocess.run(
        [*as_user, str(bindir / "pg_ctl"), "-D", data, "-l", log, "-o", opts, "-w", "start"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        pytest.skip(f"could not start Postgres: {res.stderr.strip()[:200]}")
    base = f"host=127.0.0.1 port={port} user=postgres"
    return PgServer(
        admin_dsn=f"{base} dbname=postgres",
        _base=base,
        _stop=[*as_user, str(bindir / "pg_ctl"), "-D", data, "-m", "immediate", "stop"],
        _datadir=tmp,
        _as_user=as_user,
    )


def _from_env(dsn: str) -> PgServer:
    info = conninfo_to_dict(dsn)
    info.pop("dbname", None)
    base = " ".join(f"{k}={v}" for k, v in info.items())
    return PgServer(admin_dsn=f"{base} dbname=postgres", _base=base)


@pytest.fixture(scope="session")
def pg_server() -> Iterator[PgServer]:
    env = os.environ.get("INVOICEDESK_TEST_DSN")
    server = _from_env(env) if env else _start_local()
    try:
        with psycopg.connect(server.admin_dsn, autocommit=True) as c:
            c.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(TEMPLATE_DB)))
            c.execute(sql.SQL("create database {}").format(sql.Identifier(TEMPLATE_DB)))
        apply_migrations(server.dsn_for(TEMPLATE_DB))
        yield server
    finally:
        server.stop()


@pytest.fixture
def pg_dsn(pg_server: PgServer) -> Iterator[str]:
    """DSN of a fresh, fully migrated database (cloned from the template) for one test."""
    name = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg_server.admin_dsn, autocommit=True) as c:
        c.execute(
            sql.SQL("create database {} template {}").format(
                sql.Identifier(name), sql.Identifier(TEMPLATE_DB)
            )
        )
    try:
        yield pg_server.dsn_for(name)
    finally:
        with psycopg.connect(pg_server.admin_dsn, autocommit=True) as c:
            c.execute(
                sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name))
            )


@pytest.fixture
def pg_db(pg_dsn: str) -> Iterator[Database]:
    db = Database(pg_dsn, min_size=1, max_size=4)
    try:
        yield db
    finally:
        db.close()
