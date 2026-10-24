"""QuickBooks Online OAuth 2.0: refresh-token grant, background refresh and a persisted token store.

Intuit access tokens live one hour; refresh tokens rotate (a refresh can return a new one) and
expire after ~100 days of non-use. :class:`QboTokenManager` therefore

* refreshes on demand when the access token is within ``refresh_margin`` of expiry,
* can run a daemon thread that refreshes ahead of expiry (:meth:`start_background_refresh`),
* persists every rotated refresh token in a :class:`TokenStore`, so a restart never falls back to
  a stale ``QBO_REFRESH_TOKEN`` from ``.env``.

The persisted store is :class:`PostgresTokenStore` (table ``qbo_oauth_tokens``, migration
``0500``), encrypted with pgcrypto using a key derived from the client secret, so a database dump
alone does not reveal tokens. Tokens are never logged.

Only the sandbox environment is supported (``QBO_ENVIRONMENT=sandbox``); see
:func:`invoicedesk.accounting.transport.require_sandbox`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from invoicedesk.contracts.accounting import AccountingError

__all__ = [
    "AUTHORIZE_URL",
    "SCOPE_ACCOUNTING",
    "TOKEN_URL",
    "MemoryTokenStore",
    "PostgresTokenStore",
    "QboTokenManager",
    "StoredTokens",
    "TokenSet",
    "TokenStore",
    "authorize_url",
    "exchange_authorization_code",
    "seed_fingerprint",
    "store_key_from_secret",
]

log = logging.getLogger("invoicedesk.accounting.oauth")

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"  # noqa: S105 (URL)
AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
SCOPE_ACCOUNTING = "com.intuit.quickbooks.accounting"

Clock = Callable[[], dt.datetime]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    refresh_token: str
    access_expires_at: dt.datetime
    refresh_expires_at: dt.datetime | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "access_expires_at": self.access_expires_at.isoformat(),
                "refresh_expires_at": (
                    self.refresh_expires_at.isoformat() if self.refresh_expires_at else None
                ),
            }
        )

    @classmethod
    def from_json(cls, text: str) -> TokenSet:
        d = json.loads(text)
        return cls(
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            access_expires_at=dt.datetime.fromisoformat(d["access_expires_at"]),
            refresh_expires_at=(
                dt.datetime.fromisoformat(d["refresh_expires_at"])
                if d.get("refresh_expires_at")
                else None
            ),
        )

    def __repr__(self) -> str:  # never leak tokens through repr/logging
        return (
            f"TokenSet(access_expires_at={self.access_expires_at.isoformat()}, tokens=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class StoredTokens:
    tokens: TokenSet
    #: fingerprint of the ``.env`` refresh token this chain started from; when ``.env`` gets a new
    #: token (re-bootstrap), the fingerprints differ and the store is ignored.
    seed_fingerprint: str


class TokenStore(Protocol):
    def load(self, realm_id: str) -> StoredTokens | None: ...

    def save(self, realm_id: str, stored: StoredTokens) -> None: ...


def seed_fingerprint(refresh_token: str) -> str:
    return hashlib.sha256(b"invoicedesk-qbo-seed|" + refresh_token.encode()).hexdigest()


def store_key_from_secret(client_secret: str) -> str:
    """Symmetric key for the token store, derived from the client secret (kept in ``.env``)."""
    return hashlib.sha256(b"invoicedesk-qbo-token-store|" + client_secret.encode()).hexdigest()


class MemoryTokenStore:
    """Process-local store (tests, one-off scripts)."""

    def __init__(self) -> None:
        self._rows: dict[str, StoredTokens] = {}
        self.saves = 0

    def load(self, realm_id: str) -> StoredTokens | None:
        return self._rows.get(realm_id)

    def save(self, realm_id: str, stored: StoredTokens) -> None:
        self._rows[realm_id] = stored
        self.saves += 1


class PostgresTokenStore:
    """``qbo_oauth_tokens`` row per realm, token JSON encrypted with ``pgp_sym_encrypt``."""

    def __init__(self, dsn: str, *, key: str) -> None:
        if not key:
            raise ValueError("token store key is required")
        self._dsn = dsn
        self._key = key

    def _connect(self) -> Any:
        import psycopg

        return psycopg.connect(self._dsn)

    def load(self, realm_id: str) -> StoredTokens | None:
        """``None`` when absent or undecryptable (client secret rotated): fall back to ``.env``."""
        import psycopg

        with self._connect() as conn:
            try:
                row = conn.execute(
                    "select pgp_sym_decrypt(enc_tokens, %s), seed_fingerprint"
                    " from qbo_oauth_tokens where realm_id = %s",
                    (self._key, realm_id),
                ).fetchone()
            except psycopg.errors.ExternalRoutineInvocationException:
                log.warning("qbo: stored tokens cannot be decrypted (secret changed?); ignoring")
                return None
        if row is None:
            return None
        return StoredTokens(TokenSet.from_json(row[0]), row[1])

    def save(self, realm_id: str, stored: StoredTokens) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into qbo_oauth_tokens (realm_id, environment, enc_tokens,"
                " seed_fingerprint, access_expires_at, updated_at)"
                " values (%s, 'sandbox', pgp_sym_encrypt(%s, %s), %s, %s, now())"
                " on conflict (realm_id) do update set enc_tokens = excluded.enc_tokens,"
                " seed_fingerprint = excluded.seed_fingerprint,"
                " access_expires_at = excluded.access_expires_at, updated_at = now()",
                (
                    realm_id,
                    stored.tokens.to_json(),
                    self._key,
                    stored.seed_fingerprint,
                    stored.tokens.access_expires_at,
                ),
            )


def _token_error(resp: httpx.Response) -> AccountingError:
    try:
        body = resp.json()
        err = str(body.get("error", "")) if isinstance(body, dict) else ""
    except ValueError:
        err = ""
    if resp.status_code == 429 or resp.status_code >= 500:
        return AccountingError(
            f"Intuit token endpoint unavailable ({resp.status_code})",
            retryable=True,
            status_code=resp.status_code,
        )
    hint = ""
    if err == "invalid_grant":
        hint = "; the refresh token is invalid or expired: run scripts/qbo_oauth_bootstrap.py"
    return AccountingError(
        f"Intuit token request rejected ({resp.status_code} {err or 'error'}){hint}",
        retryable=False,
        status_code=resp.status_code,
    )


def _post_token(
    http: httpx.Client, *, client_id: str, client_secret: str, data: dict[str, str]
) -> dict[str, Any]:
    try:
        resp = http.post(
            TOKEN_URL,
            data=data,
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise AccountingError(
            f"Intuit token endpoint unreachable ({type(exc).__name__})", retryable=True
        ) from exc
    if resp.status_code != 200:
        raise _token_error(resp)
    try:
        body = resp.json()
    except ValueError as exc:
        raise AccountingError(
            "Intuit token response is not JSON", retryable=True, status_code=resp.status_code
        ) from exc
    if not isinstance(body, dict) or "access_token" not in body or "refresh_token" not in body:
        raise AccountingError("Intuit token response missing tokens", retryable=False)
    return body


def _token_set(body: dict[str, Any], now: dt.datetime) -> TokenSet:
    refresh_ttl = body.get("x_refresh_token_expires_in")
    return TokenSet(
        access_token=str(body["access_token"]),
        refresh_token=str(body["refresh_token"]),
        access_expires_at=now + dt.timedelta(seconds=int(body.get("expires_in", 3600))),
        refresh_expires_at=(now + dt.timedelta(seconds=int(refresh_ttl))) if refresh_ttl else None,
    )


def authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Intuit authorization URL for the one-time auth-code flow (accounting scope only)."""
    q = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE_ACCOUNTING,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{q}"


def exchange_authorization_code(
    http: httpx.Client,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    clock: Clock = _utcnow,
) -> TokenSet:
    """``authorization_code`` grant (used once by ``scripts/qbo_oauth_bootstrap.py``)."""
    body = _post_token(
        http,
        client_id=client_id,
        client_secret=client_secret,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
    )
    return _token_set(body, clock())


class QboTokenManager:
    """Hands out a valid access token; refreshes (and persists rotation) before expiry."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        realm_id: str,
        seed_refresh_token: str,
        store: TokenStore,
        http: httpx.Client | None = None,
        clock: Clock = _utcnow,
        refresh_margin: dt.timedelta = dt.timedelta(minutes=5),
    ) -> None:
        if not (client_id and client_secret and realm_id and seed_refresh_token):
            raise ValueError("client_id, client_secret, realm_id and a refresh token are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self.realm_id = realm_id
        self._seed = seed_refresh_token
        self._seed_fp = seed_fingerprint(seed_refresh_token)
        self._store = store
        self._http = http or httpx.Client(timeout=30.0)
        self._clock = clock
        self._margin = refresh_margin
        self._lock = threading.RLock()
        self._tokens: TokenSet | None = None
        self._loaded = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.refresh_count = 0

    # ------------------------------------------------------------------ state
    def _current(self) -> TokenSet | None:
        if not self._loaded:
            self._loaded = True
            stored = self._store.load(self.realm_id)
            if stored is not None and stored.seed_fingerprint == self._seed_fp:
                self._tokens = stored.tokens
            elif stored is not None:
                log.info("qbo: .env refresh token changed since last run; using the new one")
        return self._tokens

    def _needs_refresh(self, tokens: TokenSet | None) -> bool:
        return tokens is None or tokens.access_expires_at - self._margin <= self._clock()

    @property
    def access_expires_at(self) -> dt.datetime | None:
        with self._lock:
            t = self._current()
            return t.access_expires_at if t else None

    # ------------------------------------------------------------------ API
    def access_token(self) -> str:
        with self._lock:
            tokens = self._current()
            if self._needs_refresh(tokens):
                tokens = self._refresh_locked()
            assert tokens is not None
            return tokens.access_token

    def force_refresh(self) -> str:
        """Called after a 401: the access token was revoked or expired early.

        If another process already stored a different, still-valid access token, that one is
        adopted instead of spending another refresh."""
        with self._lock:
            rejected = self._tokens.access_token if self._tokens else None
            shared = self._reload_shared()
            if (
                shared is not None
                and shared.access_token != rejected
                and not self._needs_refresh(shared)
            ):
                self._tokens = shared
                return shared.access_token
            return self._refresh_locked(reloaded=True).access_token

    def refresh_if_due(self) -> bool:
        with self._lock:
            if self._needs_refresh(self._current()):
                self._refresh_locked()
                return True
            return False

    def _reload_shared(self) -> TokenSet | None:
        """Re-read the store: the API, worker and scripts each hold a manager for the same
        realm, and whichever refreshes first rotates the refresh token for all of them."""
        self._loaded = True
        stored = self._store.load(self.realm_id)
        if stored is not None and stored.seed_fingerprint == self._seed_fp:
            self._tokens = stored.tokens
        return self._tokens

    def _grant(self, refresh_token: str) -> dict[str, Any]:
        return _post_token(
            self._http,
            client_id=self._client_id,
            client_secret=self._client_secret,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )

    def _refresh_locked(self, *, reloaded: bool = False) -> TokenSet:
        current = self._tokens if reloaded else self._reload_shared()
        if not reloaded and current is not None and not self._needs_refresh(current):
            return current  # another process refreshed (and rotated) since we last looked
        refresh_token = current.refresh_token if current else self._seed
        try:
            body = self._grant(refresh_token)
        except AccountingError as exc:
            # Lost a race: another process rotated the refresh token between our read and our
            # grant. Retry once with the token it stored.
            if exc.status_code != 400:
                raise
            latest = self._reload_shared()
            if latest is None or latest.refresh_token == refresh_token:
                raise
            if not self._needs_refresh(latest):
                return latest
            refresh_token = latest.refresh_token
            body = self._grant(refresh_token)
        tokens = _token_set(body, self._clock())
        # Persist BEFORE using: Intuit may invalidate the old refresh token once rotated.
        self._store.save(self.realm_id, StoredTokens(tokens, self._seed_fp))
        self._tokens = tokens
        self.refresh_count += 1
        log.info(
            "qbo: access token refreshed (expires %s, refresh token %s)",
            tokens.access_expires_at.isoformat(),
            "rotated" if tokens.refresh_token != refresh_token else "unchanged",
        )
        return tokens

    # ------------------------------------------------------------------ background refresh
    def start_background_refresh(self, *, interval_s: float = 60.0) -> threading.Thread:
        """Daemon thread that refreshes ahead of expiry; errors are logged and retried."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            t = threading.Thread(
                target=self._run, args=(interval_s,), name="qbo-token-refresh", daemon=True
            )
            self._thread = t
            t.start()
            return t

    def _run(self, interval_s: float) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_if_due()
            except AccountingError as exc:
                log.warning("qbo: background token refresh failed: %s", exc)
            except Exception:
                log.exception("qbo: background token refresh crashed; will retry")
            self._stop.wait(interval_s)

    def stop_background_refresh(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout)
        self._thread = None
