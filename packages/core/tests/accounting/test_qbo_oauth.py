"""OAuth token manager, token stores, sandbox guard and build_accounting_adapter."""

from __future__ import annotations

import base64
import datetime as dt
import threading
import urllib.parse

import httpx
import pytest
import respx
from invoicedesk.accounting import (
    QuickBooksAdapter,
    SandboxOnlyError,
    build_accounting_adapter,
    build_qbo_transport,
)
from invoicedesk.accounting.fake_server import REALM, make_bill
from invoicedesk.accounting.oauth import (
    TOKEN_URL,
    MemoryTokenStore,
    PostgresTokenStore,
    QboTokenManager,
    StoredTokens,
    TokenSet,
    authorize_url,
    exchange_authorization_code,
    seed_fingerprint,
    store_key_from_secret,
)
from invoicedesk.accounting.transport import (
    PRODUCTION_API_BASE,
    SANDBOX_API_BASE,
    QboTransport,
    require_sandbox,
)
from invoicedesk.config.settings import Settings
from invoicedesk.contracts.accounting import AccountingError

T0 = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC)


class Clock:
    def __init__(self, now: dt.datetime = T0) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class TokenServer:
    def __init__(self) -> None:
        self.n = 0
        self.requests: list[httpx.Request] = []
        self.fail: list[httpx.Response] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return self.fail.pop(0)
        self.n += 1
        return httpx.Response(
            200,
            json={
                "token_type": "bearer",
                "access_token": f"access-{self.n}",
                "refresh_token": f"refresh-{self.n}",
                "expires_in": 3600,
                "x_refresh_token_expires_in": 8726400,
            },
        )


@pytest.fixture
def token_server():
    ts = TokenServer()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(side_effect=ts)
        yield ts, router


def manager(store=None, clock=None, seed="refresh-seed") -> QboTokenManager:
    return QboTokenManager(
        client_id="client-abc",
        client_secret="secret-xyz",
        realm_id=REALM,
        seed_refresh_token=seed,
        store=store if store is not None else MemoryTokenStore(),
        http=httpx.Client(),
        clock=clock or Clock(),
    )


def form(req: httpx.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(req.content.decode()))


def test_refresh_grant_request_shape(token_server) -> None:
    ts, _ = token_server
    store = MemoryTokenStore()
    m = manager(store)
    assert m.access_token() == "access-1"
    (req,) = ts.requests
    assert req.method == "POST" and str(req.url) == TOKEN_URL
    assert form(req) == {"grant_type": "refresh_token", "refresh_token": "refresh-seed"}
    assert (
        req.headers["authorization"]
        == "Basic " + base64.b64encode(b"client-abc:secret-xyz").decode()
    )
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert req.headers["accept"] == "application/json"
    stored = store.load(REALM)
    assert stored is not None and stored.tokens.refresh_token == "refresh-1"
    assert stored.tokens.access_expires_at == T0 + dt.timedelta(hours=1)
    assert stored.tokens.refresh_expires_at == T0 + dt.timedelta(seconds=8726400)


def test_access_token_cached_until_margin_then_rotated_token_used(token_server) -> None:
    ts, _ = token_server
    clock = Clock()
    m = manager(clock=clock)
    assert m.access_token() == "access-1"
    clock.now = T0 + dt.timedelta(minutes=54)
    assert m.access_token() == "access-1" and len(ts.requests) == 1
    clock.now = T0 + dt.timedelta(minutes=55, seconds=1)  # inside the 5-minute margin
    assert m.access_token() == "access-2"
    assert form(ts.requests[1])["refresh_token"] == "refresh-1"  # the rotated one
    assert m.refresh_count == 2


def test_restart_uses_persisted_rotated_token_not_env(token_server) -> None:
    ts, _ = token_server
    store = MemoryTokenStore()
    clock = Clock()
    manager(store, clock).access_token()
    clock.now = T0 + dt.timedelta(hours=2)
    m2 = manager(store, clock)  # new process, same .env seed
    assert m2.access_token() == "access-2"
    assert form(ts.requests[1])["refresh_token"] == "refresh-1"


def test_restart_with_valid_stored_access_token_does_not_refresh(token_server) -> None:
    ts, _ = token_server
    store = MemoryTokenStore()
    manager(store).access_token()
    assert manager(store).access_token() == "access-1"
    assert len(ts.requests) == 1


def test_new_env_seed_overrides_store(token_server) -> None:
    ts, _ = token_server
    store = MemoryTokenStore()
    manager(store).access_token()
    manager(store, seed="refresh-bootstrapped-again").access_token()
    assert form(ts.requests[1])["refresh_token"] == "refresh-bootstrapped-again"
    assert store.load(REALM).seed_fingerprint == seed_fingerprint("refresh-bootstrapped-again")  # type: ignore[union-attr]


def test_force_refresh_always_calls_endpoint(token_server) -> None:
    ts, _ = token_server
    m = manager()
    m.access_token()
    assert m.force_refresh() == "access-2" and len(ts.requests) == 2


def test_invalid_grant_is_not_retryable(token_server) -> None:
    ts, _ = token_server
    ts.fail.append(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AccountingError) as ei:
        manager().access_token()
    assert not ei.value.retryable and ei.value.status_code == 400
    assert "qbo_oauth_bootstrap" in str(ei.value)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_token_endpoint_outage_is_retryable(token_server, status) -> None:
    ts, _ = token_server
    ts.fail.append(httpx.Response(status, text="busy"))
    with pytest.raises(AccountingError) as ei:
        manager().access_token()
    assert ei.value.retryable


def test_token_endpoint_unreachable_is_retryable() -> None:
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(side_effect=httpx.ConnectTimeout("t"))
        with pytest.raises(AccountingError) as ei:
            manager().access_token()
    assert ei.value.retryable


def test_token_response_missing_fields(token_server) -> None:
    ts, _ = token_server
    ts.fail.append(httpx.Response(200, json={"access_token": "a"}))
    with pytest.raises(AccountingError) as ei:
        manager().access_token()
    assert not ei.value.retryable


def test_background_refresh_runs_before_expiry(token_server) -> None:
    clock = Clock()
    m = manager(clock=clock)
    m.access_token()
    clock.now = T0 + dt.timedelta(minutes=58)
    t = m.start_background_refresh(interval_s=0.01)
    assert m.start_background_refresh(interval_s=0.01) is t  # idempotent
    for _ in range(200):
        if m.refresh_count >= 2:
            break
        threading.Event().wait(0.01)
    m.stop_background_refresh()
    assert m.refresh_count == 2 and not t.is_alive()
    assert m.access_expires_at == clock.now + dt.timedelta(hours=1)


def test_background_refresh_survives_errors(token_server) -> None:
    ts, _ = token_server
    ts.fail.extend([httpx.Response(503), httpx.Response(503)])
    m = manager()
    m.start_background_refresh(interval_s=0.01)
    for _ in range(300):
        if m.refresh_count >= 1:
            break
        threading.Event().wait(0.01)
    m.stop_background_refresh()
    assert m.refresh_count == 1


def test_tokens_never_in_repr() -> None:
    t = TokenSet("secret-access", "secret-refresh", T0)
    assert "secret" not in repr(t)
    assert TokenSet.from_json(t.to_json()) == t


def test_manager_requires_credentials() -> None:
    with pytest.raises(ValueError):
        QboTokenManager(
            client_id="",
            client_secret="s",
            realm_id=REALM,
            seed_refresh_token="r",
            store=MemoryTokenStore(),
        )


def test_authorize_url_and_code_exchange(token_server) -> None:
    ts, _ = token_server
    url = authorize_url(client_id="cid", redirect_uri="http://localhost:8765/callback", state="st")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith("https://appcenter.intuit.com/connect/oauth2?")
    assert q == {
        "client_id": "cid",
        "response_type": "code",
        "scope": "com.intuit.quickbooks.accounting",
        "redirect_uri": "http://localhost:8765/callback",
        "state": "st",
    }
    tokens = exchange_authorization_code(
        httpx.Client(),
        client_id="cid",
        client_secret="sec",
        code="AB11",
        redirect_uri="http://localhost:8765/callback",
        clock=Clock(),
    )
    assert tokens.refresh_token == "refresh-1"
    assert form(ts.requests[0]) == {
        "grant_type": "authorization_code",
        "code": "AB11",
        "redirect_uri": "http://localhost:8765/callback",
    }


# ---------------------------------------------------------------- sandbox guard
def test_require_sandbox() -> None:
    require_sandbox("sandbox", SANDBOX_API_BASE + "/")
    with pytest.raises(SandboxOnlyError):
        require_sandbox("production", SANDBOX_API_BASE)
    with pytest.raises(SandboxOnlyError):
        require_sandbox("sandbox", PRODUCTION_API_BASE)
    with pytest.raises(SandboxOnlyError):
        require_sandbox("sandbox", "https://sandbox-quickbooks.api.intuit.com.evil.example")


def test_transport_refuses_production_and_bad_realm() -> None:
    m = manager()
    with pytest.raises(SandboxOnlyError):
        QboTransport(
            realm_id=REALM, tokens=m, environment="production", base_url=PRODUCTION_API_BASE
        )
    with pytest.raises(SandboxOnlyError):
        QboTransport(realm_id=REALM, tokens=m, base_url=PRODUCTION_API_BASE)
    with pytest.raises(ValueError):
        QboTransport(realm_id="123/../456", tokens=m)
    with pytest.raises(ValueError):
        QboTransport(realm_id=REALM, tokens=m, max_attempts=0)


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "INVOICEDESK_MODE": "live",
        "QBO_CLIENT_ID": "cid",
        "QBO_CLIENT_SECRET": "sec",
        "QBO_REALM_ID": REALM,
        "QBO_REFRESH_TOKEN": "refresh-seed",
        "QBO_ENVIRONMENT": "sandbox",
    }
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_settings_cannot_select_production() -> None:
    with pytest.raises(ValueError):
        _settings(QBO_ENVIRONMENT="production")
    s = _settings().model_copy(update={"qbo_environment": "production"})
    with pytest.raises(SandboxOnlyError):
        build_accounting_adapter(s, store=MemoryTokenStore(), background_refresh=False)


def test_build_requires_credentials() -> None:
    with pytest.raises(RuntimeError, match="QBO_REFRESH_TOKEN"):
        build_accounting_adapter(_settings(QBO_REFRESH_TOKEN=None), store=MemoryTokenStore())


def test_build_accounting_adapter_end_to_end(token_server) -> None:
    _, router = token_server
    calls: list[httpx.Request] = []

    def bill(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "Bill": {
                    "Id": "77",
                    "SyncToken": "0",
                    "TotalAmt": 1240.00,
                    "MetaData": {"CreateTime": "2099-01-01T00:00:00-07:00"},
                }
            },
        )

    router.post(f"{SANDBOX_API_BASE}/v3/company/{REALM}/bill").mock(side_effect=bill)
    router.get(f"{SANDBOX_API_BASE}/v3/company/{REALM}/query").mock(
        return_value=httpx.Response(200, json={"QueryResponse": {}})
    )
    a = build_accounting_adapter(_settings(), store=MemoryTokenStore(), background_refresh=False)
    assert isinstance(a, QuickBooksAdapter)
    res = a.create_bill(make_bill(), idempotency_key="k1")
    assert res.external_id == "77" and res.status == "created"
    assert calls[0].headers["authorization"] == "Bearer access-1"


def test_build_with_background_refresh_starts_thread(token_server) -> None:
    t = build_qbo_transport(_settings(), store=MemoryTokenStore(), background_refresh=True)
    tokens = t._tokens  # pyright: ignore[reportPrivateUsage]
    assert isinstance(tokens, QboTokenManager)
    tokens.stop_background_refresh()


# ---------------------------------------------------------------- Postgres store
@pytest.mark.postgres
def test_postgres_token_store_round_trip_encrypted(pg_dsn: str) -> None:
    import psycopg

    store = PostgresTokenStore(pg_dsn, key=store_key_from_secret("sec"))
    assert store.load(REALM) is None
    tokens = TokenSet("access-plain-AAA", "refresh-plain-BBB", T0, T0 + dt.timedelta(days=100))
    store.save(REALM, StoredTokens(tokens, "fp1"))
    got = store.load(REALM)
    assert got == StoredTokens(tokens, "fp1")
    store.save(REALM, StoredTokens(TokenSet("a2", "r2", T0), "fp2"))
    assert store.load(REALM).tokens.refresh_token == "r2"  # type: ignore[union-attr]
    with psycopg.connect(pg_dsn) as conn:
        raw = conn.execute("select enc_tokens::text, environment from qbo_oauth_tokens").fetchall()
        assert len(raw) == 1 and raw[0][1] == "sandbox"
        assert "r2" not in raw[0][0] and "refresh" not in raw[0][0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "insert into qbo_oauth_tokens (realm_id, environment, enc_tokens, seed_fingerprint,"
                " access_expires_at) values ('1', 'production', '\\x00', 'x', now())"
            )
    wrong = PostgresTokenStore(pg_dsn, key=store_key_from_secret("other-secret"))
    assert wrong.load(REALM) is None  # secret rotated -> fall back to the .env seed
    with pytest.raises(ValueError):
        PostgresTokenStore(pg_dsn, key="")


# ---------------------------------------------------------------- several processes, one realm
def test_second_process_adopts_rotation_instead_of_using_stale_refresh_token(
    token_server,
) -> None:
    """API and worker each hold a manager. The worker refreshes (rotating the refresh token);
    when the API's access token comes due it must use the rotated token from the store."""
    ts, _ = token_server
    store, clock = MemoryTokenStore(), Clock()
    api, worker = manager(store, clock), manager(store, clock)
    assert api.access_token() == "access-1"
    clock.now += dt.timedelta(minutes=56)
    assert worker.access_token() == "access-2"  # worker refreshed with refresh-1
    # the API's token is due too: it finds the worker's fresh one and does not refresh again
    assert api.access_token() == "access-2"
    assert [form(r)["refresh_token"] for r in ts.requests] == ["refresh-seed", "refresh-1"]
    clock.now += dt.timedelta(minutes=56)
    assert api.access_token() == "access-3"
    assert form(ts.requests[-1])["refresh_token"] == "refresh-2"  # never the stale refresh-1


def test_force_refresh_adopts_token_another_process_stored(token_server) -> None:
    ts, _ = token_server
    store, clock = MemoryTokenStore(), Clock()
    api, worker = manager(store, clock), manager(store, clock)
    assert api.access_token() == "access-1"
    assert worker.access_token() == "access-1"  # shared from the store, no second grant
    assert worker.force_refresh() == "access-2"
    assert api.force_refresh() == "access-2"  # 401 on access-1: adopt the stored one
    assert len(ts.requests) == 2
    assert api.force_refresh() == "access-3"  # 401 on access-2 itself: a real refresh
    assert form(ts.requests[-1])["refresh_token"] == "refresh-2"


def test_lost_refresh_race_retries_with_rotated_token(token_server) -> None:
    ts, _ = token_server
    store, clock = MemoryTokenStore(), Clock()
    m = manager(store, clock)
    assert m.access_token() == "access-1"
    clock.now += dt.timedelta(minutes=56)

    class RacingStore(MemoryTokenStore):
        """load() before the grant sees refresh-1; the competing process then rotates."""

        def __init__(self) -> None:
            super().__init__()
            self.loads = 0

        def load(self, realm_id: str) -> StoredTokens | None:
            self.loads += 1
            if self.loads == 2:  # the reload after invalid_grant
                rotated = TokenSet("access-x", "refresh-x", clock.now - dt.timedelta(minutes=1))
                self._rows[realm_id] = StoredTokens(rotated, seed_fingerprint("refresh-seed"))
            return super().load(realm_id)

    racing = RacingStore()
    racing.save(REALM, store.load(REALM))  # type: ignore[arg-type]
    m._store = racing  # pyright: ignore[reportPrivateUsage]
    ts.fail.append(httpx.Response(400, json={"error": "invalid_grant"}))
    assert m.access_token() == "access-2"
    assert [form(r)["refresh_token"] for r in ts.requests[-2:]] == ["refresh-1", "refresh-x"]


def test_lost_race_with_fresh_stored_token_adopts_it(token_server) -> None:
    ts, _ = token_server
    clock = Clock()

    class Store(MemoryTokenStore):
        def __init__(self) -> None:
            super().__init__()
            self.loads = 0

        def load(self, realm_id: str) -> StoredTokens | None:
            self.loads += 1
            if self.loads == 3:  # 1: first use, 2: pre-grant reload, 3: after invalid_grant
                fresh = TokenSet("access-y", "refresh-y", clock.now + dt.timedelta(hours=1))
                self._rows[realm_id] = StoredTokens(fresh, seed_fingerprint("refresh-seed"))
            return super().load(realm_id)

    m = manager(Store(), clock)
    ts.fail.append(httpx.Response(400, json={"error": "invalid_grant"}))
    assert m.access_token() == "access-y"
    assert len(ts.requests) == 1


def test_invalid_grant_without_newer_stored_token_is_final(token_server) -> None:
    ts, _ = token_server
    ts.fail.append(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AccountingError) as ei:
        manager().access_token()
    assert not ei.value.retryable and "qbo_oauth_bootstrap" in str(ei.value)
    assert len(ts.requests) == 1


def test_token_endpoint_non_json_200_is_a_retryable_error(token_server) -> None:
    ts, _ = token_server
    ts.fail.append(httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(AccountingError) as ei:
        manager().access_token()
    assert ei.value.retryable
