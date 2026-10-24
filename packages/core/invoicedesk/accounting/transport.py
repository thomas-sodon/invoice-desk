"""HTTP transport for the QuickBooks Online Accounting API (sandbox only).

Shared by the least-privilege posting adapter (:mod:`invoicedesk.accounting.quickbooks`) and the
demo-only sandbox admin helper (:mod:`invoicedesk.accounting.sandbox_admin`). It is never handed
to the pipeline or the MCP server: the adapter keeps it in a private attribute and exposes only
``create_bill`` and ``attach_file``.

Responsibilities:

* **Sandbox guard.** :func:`require_sandbox` refuses any environment other than ``sandbox`` and
  any base URL other than the sandbox host (CLAUDE.md: never connect a production realm).
* **Auth.** Bearer token from an :class:`AccessTokenProvider`; on a 401 the token is refreshed
  once and the request replayed (same ``requestid``, so it is safe). A second 401 is final.
* **Error mapping** to :class:`AccountingError`: 429, 5xx, timeouts and connection errors are
  retryable; 400 (validation), 403 and other 4xx are not; 401 after a refresh is not.
* **Bounded retries** of retryable failures with exponential backoff. Every retry reuses the
  same ``requestid``, which QBO uses to return the original response instead of acting twice.
* **Exact money JSON.** :func:`encode_json` writes :class:`~decimal.Decimal` values as JSON
  number literals digit-for-digit (no float round trip), and responses are parsed with
  ``parse_float=Decimal``.
* **Masked logs.** Only method, path, status and Intuit's ``intuit_tid`` are logged; fault
  messages pass through :func:`invoicedesk.masking.mask_text`.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

import httpx

from invoicedesk.contracts.accounting import AccountingError
from invoicedesk.masking import mask_text

__all__ = [
    "MINOR_VERSION",
    "PRODUCTION_API_BASE",
    "SANDBOX_API_BASE",
    "AccessTokenProvider",
    "QboApiError",
    "QboFault",
    "QboTransport",
    "SandboxOnlyError",
    "cents_to_decimal",
    "decimal_to_cents",
    "encode_json",
    "parse_json",
    "require_sandbox",
]

log = logging.getLogger("invoicedesk.accounting.qbo")

SANDBOX_API_BASE = "https://sandbox-quickbooks.api.intuit.com"
PRODUCTION_API_BASE = "https://quickbooks.api.intuit.com"
#: Pinned Accounting API minor version (Intuit's base minor version since 2025).
MINOR_VERSION = 75

_REALM_RE = re.compile(r"^[0-9]{1,32}$")


class SandboxOnlyError(ValueError):
    """Raised when anything other than the QBO sandbox is configured."""


def require_sandbox(environment: str, base_url: str) -> None:
    if environment != "sandbox":
        raise SandboxOnlyError(
            f"QBO environment {environment!r} refused: InvoiceDesk only connects to the sandbox"
        )
    if base_url.rstrip("/") != SANDBOX_API_BASE:
        raise SandboxOnlyError(
            f"QBO base URL {base_url!r} refused: only {SANDBOX_API_BASE} is allowed"
        )


# ---------------------------------------------------------------------- money <-> JSON
def cents_to_decimal(cents: int) -> Decimal:
    """``124000 -> Decimal('1240.00')`` exactly (scaleb only moves the exponent)."""
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError("amounts must be integer cents")
    return Decimal(cents).scaleb(-2)


def decimal_to_cents(value: Decimal | int | str) -> int:
    """Exact inverse of :func:`cents_to_decimal`; refuses sub-cent values."""
    d = Decimal(str(value)) if not isinstance(value, Decimal) else value
    scaled = d.scaleb(2)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{value!r} is not a whole number of cents")
    return int(scaled)


def encode_json(obj: Any) -> str:
    """``json.dumps`` that renders Decimal as an exact number literal (``1240.00``)."""
    nonce = uuid.uuid4().hex
    decimals: list[Decimal] = []

    def sub(o: Any) -> Any:
        if isinstance(o, Decimal):
            if not o.is_finite():
                raise ValueError("non-finite Decimal")
            decimals.append(o)
            return f"__dec_{nonce}_{len(decimals) - 1}__"
        if isinstance(o, float):
            raise TypeError("floats are not allowed in QBO payloads; use Decimal")
        if isinstance(o, Mapping):
            return {str(k): sub(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [sub(v) for v in o]
        return o

    text = json.dumps(sub(obj), ensure_ascii=False, separators=(",", ":"))
    return re.sub(
        rf'"__dec_{nonce}_(\d+)__"',
        lambda m: format(decimals[int(m.group(1))], "f"),
        text,
    )


def parse_json(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


# ---------------------------------------------------------------------- errors
@dataclass(frozen=True, slots=True)
class QboFault:
    code: str
    message: str
    detail: str = ""
    type: str = ""

    @classmethod
    def list_from(cls, body: Any) -> list[QboFault]:
        """Parse ``{"Fault": {"Error": [...], "type": ...}}`` (also nested in responses)."""
        if not isinstance(body, Mapping):
            return []
        fault = body.get("Fault") or body.get("fault")
        if not isinstance(fault, Mapping):
            return []
        errors = fault.get("Error") or fault.get("error") or []
        ftype = str(fault.get("type", ""))
        out: list[QboFault] = []
        for e in errors if isinstance(errors, Sequence) else []:
            if isinstance(e, Mapping):
                out.append(
                    cls(
                        code=str(e.get("code", "")),
                        message=str(e.get("Message", e.get("message", ""))),
                        detail=str(e.get("Detail", e.get("detail", ""))),
                        type=ftype,
                    )
                )
        return out


class QboApiError(AccountingError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        faults: Sequence[QboFault] = (),
        intuit_tid: str | None = None,
    ) -> None:
        super().__init__(mask_text(message) or "", retryable=retryable, status_code=status_code)
        self.faults = tuple(faults)
        self.intuit_tid = intuit_tid

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(f.code for f in self.faults)


def _fault_summary(faults: Sequence[QboFault]) -> str:
    return "; ".join(
        f"[{f.code}] {f.message}" + (f": {f.detail}" if f.detail else "") for f in faults
    )


# ---------------------------------------------------------------------- transport
class AccessTokenProvider(Protocol):
    def access_token(self) -> str: ...

    def force_refresh(self) -> str: ...


@dataclass(slots=True)
class QboResponse:
    status_code: int
    body: Any
    intuit_tid: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


Method = Literal["GET", "POST"]


class QboTransport:
    def __init__(
        self,
        *,
        realm_id: str,
        tokens: AccessTokenProvider,
        http: httpx.Client | None = None,
        environment: str = "sandbox",
        base_url: str = SANDBOX_API_BASE,
        minor_version: int = MINOR_VERSION,
        max_attempts: int = 3,
        backoff_s: float = 0.5,
        max_backoff_s: float = 8.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        require_sandbox(environment, base_url)
        if not _REALM_RE.match(realm_id or ""):
            raise ValueError("QBO realm id must be numeric")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.realm_id = realm_id
        self._tokens = tokens
        self._http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._base = base_url.rstrip("/")
        self.minor_version = minor_version
        self._max_attempts = max_attempts
        self._backoff = backoff_s
        self._max_backoff = max_backoff_s
        self._sleep = sleep

    def url(self, path: str) -> str:
        return f"{self._base}/v3/company/{self.realm_id}/{path.lstrip('/')}"

    def request(
        self,
        method: Method,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_text: str | None = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        request_id: str | None = None,
    ) -> QboResponse:
        """Send one logical request (with bounded retries) and return the parsed 2xx body."""
        q: dict[str, str] = {"minorversion": str(self.minor_version)}
        if request_id is not None:
            q["requestid"] = request_id
        q.update(params or {})
        url = self.url(path)
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._send(
                    method, url, q, json_text=json_text, files=files, refreshed=refreshed
                )
            except QboApiError as exc:
                if exc.status_code == 401 and not refreshed:
                    self._tokens.force_refresh()
                    refreshed = True
                    attempt -= 1  # a token refresh is not a failed attempt
                    continue
                if exc.retryable and attempt < self._max_attempts:
                    delay = min(self._max_backoff, self._backoff * 2 ** (attempt - 1))
                    log.info(
                        "qbo %s %s: retryable failure (%s), retry %d in %.1fs",
                        method,
                        path,
                        exc.status_code or "network",
                        attempt,
                        delay,
                    )
                    self._sleep(delay)
                    continue
                raise
            return resp

    def _send(
        self,
        method: Method,
        url: str,
        params: Mapping[str, str],
        *,
        json_text: str | None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None,
        refreshed: bool,
    ) -> QboResponse:
        headers = {
            "Authorization": f"Bearer {self._tokens.access_token()}",
            "Accept": "application/json",
        }
        content: bytes | None = None
        if json_text is not None:
            headers["Content-Type"] = "application/json"
            content = json_text.encode("utf-8")
        try:
            resp = self._http.request(
                method, url, params=params, headers=headers, content=content, files=files
            )
        except httpx.TimeoutException as exc:
            raise QboApiError(
                f"QBO request timed out ({type(exc).__name__})", retryable=True
            ) from exc
        except httpx.TransportError as exc:
            raise QboApiError(
                f"QBO connection error ({type(exc).__name__})", retryable=True
            ) from exc

        tid = resp.headers.get("intuit_tid")
        path = url.split("/v3/company/", 1)[-1].split("/", 1)[-1]
        log.info("qbo %s %s -> %d (intuit_tid=%s)", method, path, resp.status_code, tid)
        try:
            body = parse_json(resp.text) if resp.content else None
        except ValueError:
            body = None
        faults = QboFault.list_from(body)

        if resp.status_code == 401:
            raise QboApiError(
                "QBO rejected the access token (401)"
                + (" after a token refresh" if refreshed else ""),
                retryable=False,
                status_code=401,
                faults=faults,
                intuit_tid=tid,
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise QboApiError(
                f"QBO unavailable ({resp.status_code}) {_fault_summary(faults)}".strip(),
                retryable=True,
                status_code=resp.status_code,
                faults=faults,
                intuit_tid=tid,
            )
        if resp.status_code >= 400:
            raise QboApiError(
                f"QBO rejected the request ({resp.status_code}) {_fault_summary(faults)}".strip(),
                retryable=False,
                status_code=resp.status_code,
                faults=faults,
                intuit_tid=tid,
            )
        if faults:  # a 200 that carries a Fault is still a failure
            retryable = any(f.type.lower() == "systemfault" for f in faults)
            raise QboApiError(
                f"QBO returned a fault: {_fault_summary(faults)}",
                retryable=retryable,
                status_code=resp.status_code,
                faults=faults,
                intuit_tid=tid,
            )
        if body is None:
            raise QboApiError(
                f"QBO returned an unreadable body ({resp.status_code})",
                retryable=False,
                status_code=resp.status_code,
                intuit_tid=tid,
            )
        return QboResponse(resp.status_code, body, tid, dict(resp.headers))
