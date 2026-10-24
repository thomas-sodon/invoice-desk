#!/usr/bin/env python3
"""One-time QuickBooks Online sandbox authorization (OAuth 2.0 authorization-code flow).

1. Create an app at developer.intuit.com (this provisions a sandbox company) and add
   ``http://localhost:8765/callback`` as a redirect URI for its Development keys.
2. Put ``QBO_CLIENT_ID`` and ``QBO_CLIENT_SECRET`` in ``.env`` (``QBO_ENVIRONMENT=sandbox``).
3. Run ``uv run python scripts/qbo_oauth_bootstrap.py`` (or ``make qbo-bootstrap``), sign in,
   pick the sandbox company.
4. The script prints ``QBO_REALM_ID=...`` and ``QBO_REFRESH_TOKEN=...``. Paste them into ``.env``
   yourself. The script never writes files and the token must never be committed.

After that the app refreshes and rotates tokens on its own (``invoicedesk.accounting.oauth``).
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass

import httpx
from invoicedesk.accounting.oauth import authorize_url, exchange_authorization_code
from invoicedesk.accounting.transport import SANDBOX_API_BASE, require_sandbox
from invoicedesk.config.settings import Settings, get_settings


@dataclass(frozen=True, slots=True)
class Callback:
    code: str
    realm_id: str


class CallbackError(ValueError):
    pass


def parse_callback(path: str, *, expected_state: str) -> Callback:
    """Validate the redirect Intuit sends back (state, code, realmId)."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    if "error" in q:
        raise CallbackError(f"authorization denied: {q['error'][0]}")
    state = (q.get("state") or [""])[0]
    if not secrets.compare_digest(state, expected_state):
        raise CallbackError("state mismatch (possible CSRF); start again")
    code = (q.get("code") or [""])[0]
    realm = (q.get("realmId") or [""])[0]
    if not code or not realm.isdigit():
        raise CallbackError("callback is missing code or realmId")
    return Callback(code, realm)


def wait_for_callback(host: str, port: int, path: str, timeout_s: float) -> str:
    """Serve one request on localhost and return its raw path."""
    received: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if not self.path.startswith(path):
                self.send_response(404)
                self.end_headers()
                return
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"InvoiceDesk: authorization received. You can close this tab.\n")

        def log_message(self, format: str, *args: object) -> None:  # silence (no codes in logs)
            return

    server = http.server.HTTPServer((host, port), Handler)
    server.timeout = 1.0
    stop = threading.Event()
    timer = threading.Timer(timeout_s, stop.set)
    timer.start()
    try:
        while not received and not stop.is_set():
            server.handle_request()
    finally:
        timer.cancel()
        server.server_close()
    if not received:
        raise CallbackError("timed out waiting for the Intuit redirect")
    return received[0]


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    ap = argparse.ArgumentParser(description="QBO sandbox OAuth bootstrap (prints a refresh token)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--redirect-uri", default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)

    s = settings or get_settings()
    require_sandbox(s.qbo_environment, SANDBOX_API_BASE)
    if not s.qbo_client_id or not s.qbo_client_secret:
        print("Set QBO_CLIENT_ID and QBO_CLIENT_SECRET in .env first.", file=sys.stderr)
        return 2
    redirect = args.redirect_uri or f"http://localhost:{args.port}/callback"
    state = secrets.token_urlsafe(24)
    url = authorize_url(client_id=s.qbo_client_id, redirect_uri=redirect, state=state)
    print(f"Open this URL, sign in and pick your SANDBOX company:\n\n  {url}\n")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        raw = wait_for_callback(
            "127.0.0.1", args.port, urllib.parse.urlsplit(redirect).path, args.timeout
        )
        cb = parse_callback(raw, expected_state=state)
    except CallbackError as exc:
        print(f"qbo-bootstrap: {exc}", file=sys.stderr)
        return 1
    with httpx.Client(timeout=30.0) as http:
        tokens = exchange_authorization_code(
            http,
            client_id=s.qbo_client_id,
            client_secret=s.qbo_client_secret.get_secret_value(),
            code=cb.code,
            redirect_uri=redirect,
        )
    print("Add these lines to .env (never commit .env):\n")
    print(f"QBO_REALM_ID={cb.realm_id}")
    print(f"QBO_REFRESH_TOKEN={tokens.refresh_token}")
    print("QBO_ENVIRONMENT=sandbox")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
