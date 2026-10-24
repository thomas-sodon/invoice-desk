"""scripts/qbo_oauth_bootstrap.py: callback validation and the full local flow (mocked Intuit)."""

from __future__ import annotations

import importlib.util
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx
from invoicedesk.accounting.oauth import TOKEN_URL
from invoicedesk.config.settings import Settings

REPO = Path(__file__).resolve().parents[4]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_script_qbo_bootstrap", REPO / "scripts" / "qbo_oauth_bootstrap.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_parse_callback_validates_state_code_realm() -> None:
    m = load()
    cb = m.parse_callback(
        "/callback?code=AB11&state=s1&realmId=9341453050012345", expected_state="s1"
    )
    assert (cb.code, cb.realm_id) == ("AB11", "9341453050012345")
    for bad in (
        "/callback?code=AB11&state=WRONG&realmId=1",
        "/callback?state=s1&realmId=1",
        "/callback?code=x&state=s1&realmId=abc",
        "/callback?error=access_denied&state=s1",
    ):
        with pytest.raises(m.CallbackError):
            m.parse_callback(bad, expected_state="s1")


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_full_flow_prints_refresh_token_and_writes_nothing(tmp_path, capsys, monkeypatch) -> None:
    m = load()
    monkeypatch.chdir(tmp_path)
    port = _free_port()
    captured: dict[str, str] = {}

    def fake_browser_hit(url: str) -> bool:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        captured.update(q)

        def hit() -> None:
            cb = f"http://127.0.0.1:{port}/callback?code=AB11&state={q['state']}&realmId=4620816365"
            for _ in range(50):
                try:
                    urllib.request.urlopen(cb, timeout=2).read()
                    return
                except OSError:
                    threading.Event().wait(0.05)

        threading.Thread(target=hit, daemon=True).start()
        return True

    monkeypatch.setattr(m.webbrowser, "open", fake_browser_hit)
    s = Settings(_env_file=None, QBO_CLIENT_ID="cid", QBO_CLIENT_SECRET="sec")  # type: ignore[call-arg]
    with respx.mock() as router:  # the local callback uses urllib, not httpx
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "a", "refresh_token": "RT-new", "expires_in": 3600}
            )
        )
        rc = m.main(["--port", str(port), "--timeout", "20"], settings=s)
    assert rc == 0
    out = capsys.readouterr().out
    assert "QBO_REALM_ID=4620816365" in out and "QBO_REFRESH_TOKEN=RT-new" in out
    assert captured["scope"] == "com.intuit.quickbooks.accounting"
    assert captured["redirect_uri"] == f"http://localhost:{port}/callback"
    assert list(tmp_path.iterdir()) == []  # never writes .env or any file


def test_requires_client_credentials(capsys) -> None:
    m = load()
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert m.main(["--no-browser"], settings=s) == 2
