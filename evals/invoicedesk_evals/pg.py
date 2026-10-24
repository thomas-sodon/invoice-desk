"""Postgres for eval runs: a throwaway local cluster, or an existing server.

Each evaluated document gets its own *workspace* database cloned from a migrated, seeded
template, so a document is checked for duplicates only against the seeded history and its
declared priors (exactly the semantics of the gold labels), and documents can run in parallel.
Results (``eval_runs``/``eval_results``) go to a separate results database.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

__all__ = ["PgHost", "PostgresUnavailable", "find_pg_bin", "start_ephemeral"]


class PostgresUnavailable(RuntimeError):
    pass


def find_pg_bin() -> str | None:
    for cand in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if os.path.exists(os.path.join(cand, "initdb")) and os.path.exists(
            os.path.join(cand, "pg_ctl")
        ):
            return cand
    found = shutil.which("initdb")
    return os.path.dirname(found) if found else None


@dataclass
class PgHost:
    base: str  # conninfo without dbname
    _stop: list[str] | None = None
    _datadir: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_dsn(cls, dsn: str) -> PgHost:
        info = conninfo_to_dict(dsn)
        info.pop("dbname", None)
        return cls(base=" ".join(f"{k}={v}" for k, v in info.items()))

    @property
    def admin_dsn(self) -> str:
        return self.dsn_for("postgres")

    def dsn_for(self, dbname: str) -> str:
        return f"{self.base} dbname={dbname}"

    def create_db(self, name: str, *, template: str | None = None) -> str:
        # CREATE DATABASE ... TEMPLATE fails if another session touches the template at the
        # same moment, so clones are serialized (they take ~100 ms).
        with self._lock, psycopg.connect(self.admin_dsn, autocommit=True) as c:
            if template:
                c.execute(
                    sql.SQL("create database {} template {}").format(
                        sql.Identifier(name), sql.Identifier(template)
                    )
                )
            else:
                c.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        return self.dsn_for(name)

    def drop_db(self, name: str) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as c:
            c.execute(
                sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name))
            )

    def unique_name(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def stop(self) -> None:
        if self._stop:
            subprocess.run(self._stop, check=False, capture_output=True)  # noqa: S603
        if self._datadir:
            shutil.rmtree(self._datadir, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_ephemeral() -> PgHost:
    """``initdb`` + ``pg_ctl start`` a cluster under a temp dir (as the ``postgres`` OS user
    when running as root). Raises :class:`PostgresUnavailable` when that is not possible."""
    bindir = find_pg_bin()
    if bindir is None:
        raise PostgresUnavailable("Postgres binaries (initdb/pg_ctl) not found")
    tmp = tempfile.mkdtemp(prefix="invoicedesk-eval-pg-")
    data = os.path.join(tmp, "data")
    as_user: list[str] = []
    if os.geteuid() == 0:
        if shutil.which("runuser") is None:
            raise PostgresUnavailable("running as root without runuser")
        shutil.chown(tmp, user="postgres")
        os.chmod(tmp, 0o700)
        as_user = ["runuser", "-u", "postgres", "--"]
    port = _free_port()
    init = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            *as_user,
            os.path.join(bindir, "initdb"),
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
        capture_output=True,
        text=True,
        check=False,
    )
    if init.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise PostgresUnavailable(f"initdb failed: {init.stderr.strip()[:200]}")
    opts = (
        f"-p {port} -k {tmp} -c listen_addresses=127.0.0.1 -c fsync=off "
        "-c full_page_writes=off -c synchronous_commit=off -c max_connections=200"
    )
    res = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            *as_user,
            os.path.join(bindir, "pg_ctl"),
            "-D",
            data,
            "-l",
            os.path.join(tmp, "pg.log"),
            "-o",
            opts,
            "-w",
            "start",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise PostgresUnavailable(f"pg_ctl start failed: {res.stderr.strip()[:200]}")
    return PgHost(
        base=f"host=127.0.0.1 port={port} user=postgres",
        _stop=[*as_user, os.path.join(bindir, "pg_ctl"), "-D", data, "-m", "immediate", "stop"],
        _datadir=tmp,
    )
