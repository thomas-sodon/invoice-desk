"""Fetch an invoice by URL for ``POST /agent/extract`` (``file_url``), with SSRF guards.

Only ``https`` URLs on the default port, whose host resolves exclusively to public addresses,
are fetched; redirects are not followed and the body is capped at ``MAX_BYTES``. Offline mode
uses :class:`OfflineFetcher`, which never touches the network.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from invoicedesk.contracts.preprocess import MAX_BYTES, RejectedInput

__all__ = ["HttpsFetcher", "OfflineFetcher", "UrlFetcher", "check_public_https_url"]

Resolver = Callable[[str], list[str]]


class UrlFetcher(Protocol):
    def fetch(self, url: str) -> tuple[bytes, str | None]:
        """Return ``(bytes, content_type)`` or raise :class:`RejectedInput`."""
        ...


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return sorted({str(i[4][0]) for i in infos})


def check_public_https_url(url: str, *, resolver: Resolver = _resolve) -> str:
    """Validate the URL; returns the host. Raises :class:`RejectedInput` when unsafe."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise RejectedInput("file_url must be an https:// URL.")
    if parts.username or parts.password:
        raise RejectedInput("file_url must not carry credentials.")
    if parts.port not in (None, 443):
        raise RejectedInput("file_url must use the default https port.")
    host = parts.hostname
    try:
        addrs = resolver(host)
    except OSError as exc:
        raise RejectedInput("file_url host could not be resolved.") from exc
    if not addrs:
        raise RejectedInput("file_url host could not be resolved.")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise RejectedInput("file_url must point at a public internet host.")
    return host


class HttpsFetcher:
    def __init__(self, *, http: httpx.Client | None = None, resolver: Resolver = _resolve) -> None:
        self._http = http or httpx.Client(timeout=20.0, follow_redirects=False)
        self._resolver = resolver

    def fetch(self, url: str) -> tuple[bytes, str | None]:
        check_public_https_url(url, resolver=self._resolver)
        try:
            with self._http.stream("GET", url, follow_redirects=False) as resp:
                if resp.status_code != 200:
                    raise RejectedInput(f"file_url returned HTTP {resp.status_code}.")
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_BYTES:
                        raise RejectedInput("The file is over the 10 MB limit.")
                return bytes(buf), resp.headers.get("content-type")
        except httpx.HTTPError as exc:
            raise RejectedInput("file_url could not be downloaded.") from exc


class OfflineFetcher:
    """Serves registered URLs from memory (tests, offline demo); everything else is refused."""

    def __init__(self, files: dict[str, tuple[bytes, str | None]] | None = None) -> None:
        self.files = dict(files or {})

    def fetch(self, url: str) -> tuple[bytes, str | None]:
        if url not in self.files:
            raise RejectedInput("Offline mode cannot download file_url; use /agent/uploads.")
        return self.files[url]
