"""Fixtures: the fake QBO sandbox (``invoicedesk.accounting.fake_server``) mounted on respx."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from invoicedesk.accounting.fake_server import REALM, FakeQbo, StaticTokens
from invoicedesk.accounting.transport import QboTransport


@pytest.fixture
def fake_qbo() -> Iterator[tuple[FakeQbo, respx.MockRouter]]:
    server = FakeQbo()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route(host="sandbox-quickbooks.api.intuit.com").mock(side_effect=server.handle)
        yield server, router


@pytest.fixture
def tokens() -> StaticTokens:
    return StaticTokens()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def transport(tokens: StaticTokens, sleeps: list[float]) -> QboTransport:
    return QboTransport(
        realm_id=REALM, tokens=tokens, http=httpx.Client(), sleep=sleeps.append, max_attempts=3
    )
