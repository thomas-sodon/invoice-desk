"""Supabase Storage implementation of :class:`invoicedesk.contracts.infra.Storage` (production).

Talks to the Storage REST API with the service-role key (server side only; never sent to a
browser). Objects live in one private bucket (``SUPABASE_STORAGE_BUCKET``, default
``invoices``); browsers get short-lived signed URLs only.

Endpoints used (``{SUPABASE_URL}/storage/v1``):

* ``POST   /object/{bucket}/{key}`` with ``x-upsert: true``   upload
* ``GET    /object/authenticated/{bucket}/{key}``             download
* ``HEAD   /object/authenticated/{bucket}/{key}``             exists
* ``DELETE /object/{bucket}`` ``{"prefixes": [key]}``         delete
* ``POST   /object/list/{bucket}`` ``{"prefix", "limit", "offset"}``  list one folder level
* ``POST   /object/sign/{bucket}/{key}`` ``{"expiresIn"}``    signed URL

Not exercised against a live project here (docs/GAPS.md); tests use ``httpx.MockTransport``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from invoicedesk.contracts.infra import StoredObject
from invoicedesk.storage import safe_key

if TYPE_CHECKING:
    from invoicedesk.config.settings import Settings

__all__ = ["SupabaseStorage", "SupabaseStorageError"]

_PAGE = 1000


class SupabaseStorageError(RuntimeError):
    pass


class SupabaseStorage:
    def __init__(
        self,
        url: str,
        service_key: str,
        *,
        bucket: str = "invoices",
        http: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base = url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = headers

    @classmethod
    def from_settings(cls, settings: Settings) -> SupabaseStorage:
        import os

        if not settings.supabase_url or settings.supabase_service_role_key is None:
            raise RuntimeError(
                "STORAGE_BACKEND=supabase needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
            )
        return cls(
            settings.supabase_url,
            settings.supabase_service_role_key.get_secret_value(),
            bucket=os.environ.get("SUPABASE_STORAGE_BUCKET", "invoices"),
        )

    # ------------------------------------------------------------------ helpers
    def _path(self, key: str) -> str:
        return quote(safe_key(key), safe="/")

    def _req(self, method: str, url: str, **kw: Any) -> httpx.Response:
        headers = {**self._headers, **kw.pop("headers", {})}
        return self._http.request(method, url, headers=headers, **kw)

    @staticmethod
    def _check(resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code >= 300:
            raise SupabaseStorageError(f"supabase storage {what} failed: HTTP {resp.status_code}")
        return resp

    # ------------------------------------------------------------------ Storage
    def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        resp = self._req(
            "POST",
            f"{self.base}/object/{self.bucket}/{self._path(key)}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true", "cache-control": "3600"},
        )
        self._check(resp, "upload")
        return StoredObject(
            key=safe_key(key),
            size=len(data),
            content_type=content_type,
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def get(self, key: str) -> bytes:
        resp = self._req("GET", f"{self.base}/object/authenticated/{self.bucket}/{self._path(key)}")
        if resp.status_code in (400, 404):
            raise KeyError(key)
        return self._check(resp, "download").content

    def delete(self, key: str) -> None:
        resp = self._req(
            "DELETE", f"{self.base}/object/{self.bucket}", json={"prefixes": [safe_key(key)]}
        )
        if resp.status_code not in (200, 204, 404):
            self._check(resp, "delete")

    def exists(self, key: str) -> bool:
        resp = self._req(
            "HEAD", f"{self.base}/object/authenticated/{self.bucket}/{self._path(key)}"
        )
        if resp.status_code == 200:
            return True
        if resp.status_code in (400, 404):
            return False
        self._check(resp, "exists")
        return False

    def _list_dir(self, prefix: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            resp = self._req(
                "POST",
                f"{self.base}/object/list/{self.bucket}",
                json={
                    "prefix": prefix,
                    "limit": _PAGE,
                    "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"},
                },
            )
            page = self._check(resp, "list").json()
            if not isinstance(page, list):
                raise SupabaseStorageError("supabase storage list returned a non-list")
            out.extend(page)
            if len(page) < _PAGE:
                return out
            offset += _PAGE

    def list(self, prefix: str) -> list[str]:
        """Every object key starting with ``prefix`` (folders are walked recursively)."""
        folder, _, name_prefix = prefix.rpartition("/")
        keys: list[str] = []
        stack = [(folder, name_prefix)]
        while stack:
            directory, starts = stack.pop()
            for entry in self._list_dir(directory):
                name = str(entry.get("name", ""))
                if not name.startswith(starts):
                    continue
                full = f"{directory}/{name}" if directory else name
                if entry.get("id") is None:  # a folder placeholder
                    stack.append((full, ""))
                else:
                    keys.append(full)
        return sorted(keys)

    def signed_url(self, key: str, *, expires_in_s: int = 3600) -> str:
        resp = self._req(
            "POST",
            f"{self.base}/object/sign/{self.bucket}/{self._path(key)}",
            json={"expiresIn": int(expires_in_s)},
        )
        body = self._check(resp, "sign").json()
        signed = body.get("signedURL") or body.get("signedUrl")
        if not isinstance(signed, str):
            raise SupabaseStorageError("supabase storage sign returned no URL")
        return signed if signed.startswith("http") else f"{self.base}{signed}"

    def close(self) -> None:
        self._http.close()
