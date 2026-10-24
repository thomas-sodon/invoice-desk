"""Storage implementations available to every component: local filesystem and in-memory.
The Supabase Storage implementation lives with the API component (invoicedesk_api)."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path, PurePosixPath

from invoicedesk.contracts.infra import StoredObject

__all__ = ["InMemoryStorage", "LocalStorage", "safe_key"]


def safe_key(key: str) -> str:
    """Reject absolute paths and traversal; return the normalized POSIX key."""
    p = PurePosixPath(key)
    if p.is_absolute() or ".." in p.parts or not key or key.endswith("/"):
        raise ValueError(f"invalid storage key: {key!r}")
    return str(p)


class InMemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        k = safe_key(key)
        self.objects[k] = (data, content_type)
        return StoredObject(
            key=k,
            size=len(data),
            content_type=content_type,
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def get(self, key: str) -> bytes:
        return self.objects[safe_key(key)][0]

    def delete(self, key: str) -> None:
        self.objects.pop(safe_key(key), None)

    def exists(self, key: str) -> bool:
        return safe_key(key) in self.objects

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def signed_url(self, key: str, *, expires_in_s: int = 3600) -> str:
        return f"memory://{safe_key(key)}?exp={int(time.time()) + expires_in_s}"


class LocalStorage:
    """Filesystem storage under ``root``. ``signed_url`` returns an API-relative URL signed with
    HMAC so the API can serve ``GET /files/{key}?exp=&sig=`` without auth lookups."""

    def __init__(self, root: Path, *, signing_secret: str, base_url: str = "") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._secret = signing_secret.encode()
        self.base_url = base_url.rstrip("/")

    def _path(self, key: str) -> Path:
        return self.root / safe_key(key)

    def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.with_name(p.name + ".ctype").write_text(content_type)
        return StoredObject(
            key=safe_key(key),
            size=len(data),
            content_type=content_type,
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def content_type(self, key: str) -> str:
        ct = self._path(key).with_name(self._path(key).name + ".ctype")
        return ct.read_text() if ct.exists() else "application/octet-stream"

    def delete(self, key: str) -> None:
        p = self._path(key)
        p.unlink(missing_ok=True)
        p.with_name(p.name + ".ctype").unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list(self, prefix: str) -> list[str]:
        out = []
        for p in self.root.rglob("*"):
            if p.is_file() and not p.name.endswith(".ctype"):
                k = p.relative_to(self.root).as_posix()
                if k.startswith(prefix):
                    out.append(k)
        return sorted(out)

    def sign(self, key: str, exp: int) -> str:
        return hmac.new(self._secret, f"{safe_key(key)}:{exp}".encode(), hashlib.sha256).hexdigest()

    def verify(self, key: str, exp: int, sig: str) -> bool:
        return exp >= int(time.time()) and hmac.compare_digest(self.sign(key, exp), sig)

    def signed_url(self, key: str, *, expires_in_s: int = 3600) -> str:
        exp = int(time.time()) + expires_in_s
        return f"{self.base_url}/files/{safe_key(key)}?exp={exp}&sig={self.sign(key, exp)}"
