"""TEST HELPER: a stateful fake of the QBO sandbox Accounting API, for respx.

Not used at runtime. Reusable by other components' tests (``respx`` must be installed).

It mimics the behaviour the adapter relies on: ``requestid`` replays return the original
response without acting again, ``MetaData.CreateTime`` stamps, ``TotalAmt`` computed from lines,
Fault bodies for errors, multipart ``/upload`` producing ``AttachableResponse``, ``/query`` and
``operation=delete``. Response shapes follow Intuit's documented JSON.
"""

from __future__ import annotations

import datetime as dt
import email.parser
import email.policy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from invoicedesk.accounting.transport import encode_json, parse_json
from invoicedesk.contracts.accounting import BillLine, BillRequest

REALM = "9341453050012345"
BASE = f"https://sandbox-quickbooks.api.intuit.com/v3/company/{REALM}"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"  # noqa: S105 (URL)
T0 = dt.datetime(2026, 10, 24, 15, 0, 0, tzinfo=dt.UTC)


def fault(
    code: str, message: str, detail: str = "", ftype: str = "ValidationFault"
) -> dict[str, Any]:
    return {
        "Fault": {
            "Error": [{"Message": message, "Detail": detail, "code": code, "element": ""}],
            "type": ftype,
        },
        "time": "2026-10-24T08:00:00.000-07:00",
    }


def parse_multipart(request: httpx.Request) -> list[dict[str, Any]]:
    ctype = request.headers["content-type"]
    raw = b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.content
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    parts = []
    for part in msg.iter_parts():
        parts.append(
            {
                "name": part.get_param("name", header="content-disposition"),
                "filename": part.get_filename(),
                "content_type": part.get_content_type(),
                "data": part.get_payload(decode=True),
            }
        )
    return parts


@dataclass
class FakeQbo:
    now: dt.datetime = T0
    bills: dict[str, dict[str, Any]] = field(default_factory=dict)
    attachables: dict[str, dict[str, Any]] = field(default_factory=dict)
    cache: dict[str, tuple[int, str]] = field(default_factory=dict)
    effects: int = 0  # number of non-replayed write effects
    next_id: int = 145
    #: queued interceptors: each returns a Response, raises, or returns None to continue
    before: list[Callable[[httpx.Request], httpx.Response | None]] = field(default_factory=list)
    #: called after the write was applied (simulate "succeeded server-side, then timed out")
    after: list[Callable[[httpx.Request], None]] = field(default_factory=list)
    valid_tokens: set[str] = field(default_factory=lambda: {"access-1"})
    requests: list[httpx.Request] = field(default_factory=list)
    #: QBO's "warn if duplicate bill number" preference (off by default in QBO, so off here)
    duplicate_docnumber_check: bool = False
    #: the adapter's DocNumber lookups (``select ... from Bill where DocNumber = ...``) are
    #: recorded in ``lookups`` instead of ``requests`` and skip the ``before``/``after`` hooks,
    #: so tests about the write path can reason about writes alone. Set False to treat them
    #: like any other request.
    lookups_passthrough: bool = True
    lookups: list[httpx.Request] = field(default_factory=list)

    def _iso(self) -> str:
        return self.now.astimezone(dt.timezone(dt.timedelta(hours=-7))).isoformat()

    def _resp(self, status: int, body: Any) -> httpx.Response:
        return httpx.Response(
            status,
            content=encode_json(body).encode(),
            headers={"content-type": "application/json", "intuit_tid": "1-abc-tid"},
        )

    @staticmethod
    def is_lookup(request: httpx.Request) -> bool:
        return (
            request.method == "GET"
            and request.url.path.endswith("/query")
            and "where DocNumber" in request.url.params.get("query", "")
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        lookup = self.lookups_passthrough and self.is_lookup(request)
        (self.lookups if lookup else self.requests).append(request)
        if self.before and not lookup:
            r = self.before.pop(0)(request)
            if r is not None:
                return r
        auth = request.headers.get("authorization", "")
        if auth.removeprefix("Bearer ") not in self.valid_tokens:
            return self._resp(401, fault("3200", "AuthenticationFailed", ftype="AUTHENTICATION"))
        path = request.url.path.split(f"/company/{REALM}/", 1)[1]
        rid = request.url.params.get("requestid")
        if rid is not None and rid in self.cache:
            status, text = self.cache[rid]
            return httpx.Response(
                status, content=text.encode(), headers={"content-type": "application/json"}
            )
        if request.method == "POST" and path == "bill":
            if request.url.params.get("operation") == "delete":
                status, body = self._delete(request)
            else:
                status, body = self._create_bill(request)
        elif request.method == "POST" and path == "upload":
            status, body = self._upload(request)
        elif request.method == "GET" and path == "query":
            status, body = self._query(request.url.params["query"])
        elif request.method == "GET" and path.startswith("bill/"):
            b = self.bills.get(path.split("/", 1)[1])
            status, body = (200, {"Bill": b}) if b else (400, fault("610", "Object Not Found"))
        else:
            status, body = 404, fault("0", "unsupported")
        text = encode_json(body)
        if rid is not None and status < 500:
            self.cache[rid] = (status, text)
        if not lookup:
            for hook in self.after:
                hook(request)
            self.after.clear()
        return httpx.Response(
            status, content=text.encode(), headers={"content-type": "application/json"}
        )

    def _create_bill(self, request: httpx.Request) -> tuple[int, dict[str, Any]]:
        payload = parse_json(request.content.decode())
        if self.duplicate_docnumber_check and any(
            b["DocNumber"] == payload["DocNumber"] for b in self.bills.values()
        ):
            return 400, fault(
                "6140",
                "Duplicate Document Number Error",
                "Duplicate Document Number Error : You must specify a different number."
                f" This number has already been used. DocNumber={payload['DocNumber']}",
            )
        self.effects += 1
        bid = str(self.next_id)
        self.next_id += 1
        total = sum((Decimal(str(line["Amount"])) for line in payload["Line"]), Decimal("0.00"))
        bill = {
            **payload,
            "Id": bid,
            "SyncToken": "0",
            "domain": "QBO",
            "sparse": False,
            "MetaData": {"CreateTime": self._iso(), "LastUpdatedTime": self._iso()},
            "TotalAmt": total,
            "Balance": total,
            "CurrencyRef": {"value": "USD", "name": "United States Dollar"},
            "APAccountRef": {"value": "33", "name": "Accounts Payable (A/P)"},
            "Line": [
                {**line, "Id": str(i + 1), "LineNum": i + 1}
                for i, line in enumerate(payload["Line"])
            ],
        }
        self.bills[bid] = bill
        return 200, {"Bill": bill, "time": self._iso()}

    def _upload(self, request: httpx.Request) -> tuple[int, dict[str, Any]]:
        parts = parse_multipart(request)
        meta = json.loads(next(p for p in parts if p["name"] == "file_metadata_01")["data"])
        content = next(p for p in parts if p["name"] == "file_content_01")
        ref = meta["AttachableRef"][0]["EntityRef"]
        if ref["type"] != "Bill" or ref["value"] not in self.bills:
            return 200, {
                "AttachableResponse": [fault("610", "Object Not Found", "Bill not found")],
                "time": self._iso(),
            }
        self.effects += 1
        aid = str(5000000000000000000 + self.next_id)
        self.next_id += 1
        att = {
            "Id": aid,
            "SyncToken": "0",
            "FileName": meta["FileName"],
            "ContentType": meta["ContentType"],
            "Size": len(content["data"]),
            "AttachableRef": meta["AttachableRef"],
            "Note": meta.get("Note", ""),
            "TempDownloadUri": f"https://intuit-qbo-sandbox.example/{aid}",
            "MetaData": {"CreateTime": self._iso(), "LastUpdatedTime": self._iso()},
            "domain": "QBO",
        }
        self.attachables[aid] = {**att, "_data": content["data"], "_part": content}
        return 200, {"AttachableResponse": [{"Attachable": att}], "time": self._iso()}

    def _query(self, q: str) -> tuple[int, dict[str, Any]]:
        m = re.search(r"where DocNumber = '((?:[^'\\]|\\.)*)'", q)
        rows = list(self.bills.values())
        if m:
            doc = m.group(1).replace("\\'", "'").replace("\\\\", "\\")
            rows = [b for b in rows if b["DocNumber"] == doc]
        pm = re.search(r"startposition (\d+) maxresults (\d+)", q)
        if pm:
            start, mx = int(pm.group(1)), int(pm.group(2))
            rows = rows[start - 1 : start - 1 + mx]
        body: dict[str, Any] = {"QueryResponse": {}, "time": self._iso()}
        if rows:
            body["QueryResponse"] = {"Bill": rows, "startPosition": 1, "maxResults": len(rows)}
        return 200, body

    def _delete(self, request: httpx.Request) -> tuple[int, dict[str, Any]]:
        payload = parse_json(request.content.decode())
        b = self.bills.get(payload["Id"])
        if b is None:
            return 400, fault("610", "Object Not Found")
        if payload["SyncToken"] != b["SyncToken"]:
            return 400, fault("5010", "Stale Object Error")
        del self.bills[payload["Id"]]
        self.effects += 1
        return 200, {
            "Bill": {"status": "Deleted", "domain": "QBO", "Id": payload["Id"]},
            "time": self._iso(),
        }


class StaticTokens:
    def __init__(self, tokens: list[str] | None = None) -> None:
        self._tokens = tokens or ["access-1", "access-2"]
        self.refreshes = 0
        self.current = self._tokens[0]

    def access_token(self) -> str:
        return self.current

    def force_refresh(self) -> str:
        self.refreshes += 1
        self.current = self._tokens[min(self.refreshes, len(self._tokens) - 1)]
        return self.current


def make_bill(**overrides: Any) -> BillRequest:
    base: dict[str, Any] = {
        "vendor_ref": "56",
        "doc_number": "INV-2231",
        "txn_date": dt.date(2026, 10, 2),
        "due_date": dt.date(2026, 11, 1),
        "lines": (
            BillLine(
                description="Replace kitchen faucet, Unit 12B",
                amount_cents=98000,
                expense_account_ref="7",
            ),
            BillLine(description="Parts", amount_cents=26000, expense_account_ref="7"),
        ),
        "total_cents": 124000,
        "memo": "WO-1187 Unit 12B",
    }
    base.update(overrides)
    return BillRequest(**base)
