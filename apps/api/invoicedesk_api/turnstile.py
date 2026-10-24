"""Cloudflare Turnstile verification for the try-it page (M14).

Live: ``POST https://challenges.cloudflare.com/turnstile/v0/siteverify`` with the secret, the
token and the visitor IP (form-encoded); the token passes only when ``success`` is true.

Offline (tests, local demo) never calls Cloudflare. It accepts tokens only when the configured
secret is Cloudflare's documented **always-pass test secret** (also the default when no secret
is set), and mirrors the documented always-fail / already-spent test secrets, so the failure
path can be exercised without the network:
https://developers.cloudflare.com/turnstile/troubleshooting/testing/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

__all__ = [
    "SITEVERIFY_URL",
    "TEST_SECRET_ALWAYS_FAILS",
    "TEST_SECRET_ALWAYS_PASSES",
    "TEST_SECRET_TOKEN_SPENT",
    "CloudflareTurnstile",
    "OfflineTurnstile",
    "TurnstileResult",
    "TurnstileVerifier",
]

log = logging.getLogger("invoicedesk.api.turnstile")

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TEST_SECRET_ALWAYS_PASSES = "1x0000000000000000000000000000000AA"  # noqa: S105 - public
TEST_SECRET_ALWAYS_FAILS = "2x0000000000000000000000000000000AA"  # noqa: S105 - public
TEST_SECRET_TOKEN_SPENT = "3x0000000000000000000000000000000AA"  # noqa: S105 - public
MAX_TOKEN_LEN = 2048  # Cloudflare documents tokens up to 2048 characters


@dataclass(frozen=True, slots=True)
class TurnstileResult:
    success: bool
    error_codes: tuple[str, ...] = ()


class TurnstileVerifier(Protocol):
    def verify(self, token: str, *, remote_ip: str | None) -> TurnstileResult: ...


def _token_ok(token: str) -> bool:
    return bool(token) and len(token) <= MAX_TOKEN_LEN


class OfflineTurnstile:
    """No network. Behaves like Cloudflare does for its documented test secrets."""

    def __init__(self, secret: str | None) -> None:
        self.secret = secret or TEST_SECRET_ALWAYS_PASSES
        self.calls: list[str] = []

    def verify(self, token: str, *, remote_ip: str | None) -> TurnstileResult:
        self.calls.append(token)
        if not _token_ok(token):
            return TurnstileResult(False, ("missing-input-response",))
        if self.secret == TEST_SECRET_ALWAYS_PASSES:
            return TurnstileResult(True)
        if self.secret == TEST_SECRET_TOKEN_SPENT:
            return TurnstileResult(False, ("timeout-or-duplicate",))
        # Always-fail secret, or a real secret while offline: fail closed (nothing to call).
        return TurnstileResult(False, ("invalid-input-response",))


class CloudflareTurnstile:
    """Real siteverify call (live mode). Any transport error fails closed."""

    def __init__(
        self, secret: str, *, http: httpx.Client | None = None, url: str = SITEVERIFY_URL
    ) -> None:
        self.secret = secret
        self.url = url
        self._http = http or httpx.Client(timeout=10.0)

    def verify(self, token: str, *, remote_ip: str | None) -> TurnstileResult:
        if not _token_ok(token):
            return TurnstileResult(False, ("missing-input-response",))
        data = {"secret": self.secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        try:
            resp = self._http.post(self.url, data=data)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("turnstile siteverify failed: %s", type(exc).__name__)
            return TurnstileResult(False, ("network-error",))
        if resp.status_code != 200 or not isinstance(body, dict):
            return TurnstileResult(False, (f"http-{resp.status_code}",))
        codes = tuple(str(c) for c in body.get("error-codes", []) or [])
        return TurnstileResult(bool(body.get("success") is True), codes)


def build_turnstile(
    mode: Literal["offline", "live"], secret: str | None, *, http: httpx.Client | None = None
) -> TurnstileVerifier:
    if mode == "live":
        if not secret:
            # Fail closed: a live deployment without a secret accepts nobody.
            return OfflineTurnstile(TEST_SECRET_ALWAYS_FAILS)
        return CloudflareTurnstile(secret, http=http)
    return OfflineTurnstile(secret)
