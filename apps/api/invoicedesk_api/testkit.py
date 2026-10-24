"""Test kit for the API (used by ``apps/api/tests``; the app never imports it).

Builds an environment on a migrated throwaway Postgres: the seeded Maple Row universe, the real
preprocessor, guardrails, extractor and rules engine, and offline fakes for everything external
(OracleLLM, FakeAccounting, LocalStorage, mocked HTTP). The secrets below are test-only values.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi.testclient import TestClient
from invoicedesk.config.settings import REPO_ROOT, Settings
from invoicedesk.db.connection import Database
from invoicedesk.fakes import FakeAccounting
from pydantic import SecretStr

from invoicedesk_api.main import create_app
from invoicedesk_api.runtime import Runtime, build_runtime, load_oracle
from invoicedesk_api.settings import ApiSettings

JWT_SECRET = "test-supabase-jwt-secret-0123456789abcdef"  # noqa: S105 - test-only
AGENT_SECRET = "test-agent-jwt-secret-0123456789abcdef"  # noqa: S105 - test-only
INGEST_SECRET = "test-ingest-secret"  # noqa: S105 - test-only
SESSION_SECRET = "test-session-secret"  # noqa: S105 - test-only
CLERK = "dana.clerk@maplerow.example"
APPROVER = "priya.approver@maplerow.example"
OWNER = "sam.owner@maplerow.example"

#: Remit-to account numbers printed in the demo set; none may ever leave the API unmasked.
DEMO_ACCOUNT_NUMBERS = ("000555061234", "000555067890", "000555077001", "110000000")
_LONG_DIGITS = re.compile(r"(?<![\w.\-+:])\d{6,}(?![\w.\-])")
_HEXISH = re.compile(r"^[0-9a-f]{32,}$")


_MONEY = {"subtotal", "tax", "total"}


def unmasked_numbers(obj: Any) -> list[str]:
    """Every string in a JSON value that still carries a 6+ digit run (an account-like number).

    Skipped on purpose: signed URLs (expiry timestamps), hashes, and money field values, which
    are integer cents by contract (``{"name": "total", "value": "124000"}``)."""
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "value" and obj.get("name") in _MONEY:
                continue
            found.extend(unmasked_numbers(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(unmasked_numbers(v))
    elif (
        isinstance(obj, str)
        and not _HEXISH.match(obj)
        and not obj.startswith("http")
        and _LONG_DIGITS.search(obj)
    ):
        found.append(obj)
    return found


class RecordingClient(TestClient):
    """TestClient that keeps every JSON body it received (for the bank-number scan)."""

    bodies: list[Any]

    def request(self, *args: Any, **kwargs: Any) -> Any:
        resp = super().request(*args, **kwargs)
        if resp.headers.get("content-type", "").startswith("application/json"):
            self.bodies.append(resp.json())
        return resp


@dataclass
class Env:
    rt: Runtime
    client: RecordingClient
    db: Database
    accounting: FakeAccounting
    webhook_calls: list[dict[str, Any]] = field(default_factory=list)
    staff: dict[str, str] = field(default_factory=dict)

    def dev(self, email: str) -> dict[str, str]:
        return {"X-Dev-User": email}

    def bearer(self, email: str, *, secret: str = JWT_SECRET, ttl: int = 600) -> dict[str, str]:
        token = jwt.encode(
            {
                "sub": self.staff[email],
                "aud": "authenticated",
                "exp": dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl),
                "role": "authenticated",
            },
            secret,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    def agent(
        self, scopes: str, *, client: str = "claude-desktop", ttl: int = 600
    ) -> dict[str, str]:
        token = jwt.encode(
            {
                "scope": scopes,
                "act": {"sub": client},
                "aud": "invoicedesk-api",
                "exp": dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl),
            },
            AGENT_SECRET,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    def drain(self) -> int:
        return self.rt.run_pending_jobs()

    def status(self, document_id: str) -> str:
        with self.db.tx() as conn:
            row = conn.execute(
                "select status::text as s from documents where id = %s", (document_id,)
            ).fetchone()
        assert row is not None
        return row["s"]

    def sql(self, query: Any, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.db.tx() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]


def make_settings(dsn: str, storage_dir: Path, **overrides: Any) -> Settings:
    base = Settings()  # pyright: ignore[reportCallIssue]
    return base.model_copy(
        update={
            "mode": "offline",
            "tenant": "maple_row",
            "database_url": dsn,
            "tenants_dir": REPO_ROOT / "tenants",
            "storage_backend": "local",
            "storage_local_dir": storage_dir,
            "replay_dir": storage_dir / "no-replays",
            "supabase_jwt_secret": SecretStr(JWT_SECRET),
            "ingest_webhook_secret": SecretStr(INGEST_SECRET),
            "session_signing_secret": SecretStr(SESSION_SECRET),
            "turnstile_secret_key": None,
            "api_base_url": "http://testserver",
            "langfuse_host": None,
            **overrides,
        }
    )


def make_api_settings(**overrides: Any) -> ApiSettings:
    base = ApiSettings()  # pyright: ignore[reportCallIssue]
    return base.model_copy(
        update={
            "pipeline_mode": "embedded",
            "embedded_worker": False,
            "agent_jwt_secret": SecretStr(AGENT_SECRET),
            "n8n_followup_webhook_url": None,
            "report_card_dir": Path("/nonexistent-report-cards"),
            **overrides,
        }
    )


def build_env(
    db: Database,
    storage_dir: Path,
    *,
    settings: dict[str, Any] | None = None,
    api: dict[str, Any] | None = None,
    seed: bool = True,
    **runtime_kw: Any,
) -> Env:
    from invoicedesk.storage import LocalStorage

    s = make_settings(db.dsn, storage_dir, **(settings or {}))
    a = make_api_settings(**(api or {}))
    webhook_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        webhook_calls.append(
            {"url": str(request.url), "json": json.loads(request.content or b"{}")}
        )
        return httpx.Response(200, json={"ok": True})

    oracle = runtime_kw.pop("llm", None) or load_oracle()
    accounting = FakeAccounting()
    rt = build_runtime(
        s,
        a,
        db=db,
        storage=LocalStorage(
            storage_dir, signing_secret=SESSION_SECRET, base_url="http://testserver"
        ),
        llm=oracle,
        replay_llm=runtime_kw.pop("replay_llm", None) or oracle,
        accounting=accounting,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        **runtime_kw,
    )
    if seed:
        from invoicedesk_data.seed import seed_database

        seed_database(db, "maple_row")
    client = RecordingClient(create_app(rt, start_jobs=False))
    client.bodies = []
    with db.tx() as conn:
        staff = {
            r["email"]: str(r["id"])
            for r in conn.execute("select id, email from staff_users").fetchall()
        }
    return Env(
        rt=rt, client=client, db=db, accounting=accounting, webhook_calls=webhook_calls, staff=staff
    )


def demo_file(name_prefix: str, *, include_email: bool = False) -> Any:
    from invoicedesk_data.demo import demo_files

    return next(
        f for f in demo_files(include_email=include_email) if f.filename.startswith(name_prefix)
    )
