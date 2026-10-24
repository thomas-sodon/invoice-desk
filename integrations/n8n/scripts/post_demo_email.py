"""Send the demo bank-change e-mail to ``POST /api/v1/ingest/email`` the way n8n does.

Use this when there is no IMAP mailbox to point the ``email-ingest.json`` workflow at: it builds
the same payload as the workflow's "Build signed payload" node (``data/demo/bank-change-email.json``
plus its PDF attachment, base64) and signs the exact body bytes with ``INGEST_WEBHOOK_SECRET``.

    uv run python integrations/n8n/scripts/post_demo_email.py --dry-run
    uv run python integrations/n8n/scripts/post_demo_email.py --api http://localhost:8000

The secret is read from the environment (``.env`` is not parsed here). Nothing is printed that
would reveal the secret or the attachment bytes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EMAIL = REPO_ROOT / "data" / "demo" / "bank-change-email.json"
ALLOWED_TYPES = frozenset(
    {"application/pdf", "image/jpeg", "image/png", "image/heic", "image/heif"}
)
MAX_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 10


def build_payload(email_json: Path) -> dict[str, Any]:
    """The ingest payload for a demo e-mail fixture (same shape the n8n workflow produces)."""
    src = json.loads(email_json.read_text(encoding="utf-8"))
    attachments: list[dict[str, str]] = []
    for rel in src.get("attachments", []):
        path = (email_json.parent / rel).resolve()
        if email_json.parent.resolve() not in path.parents:
            raise ValueError(f"attachment escapes the fixture directory: {rel}")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        if ctype not in ALLOWED_TYPES or len(data) > MAX_BYTES:
            continue
        if len(attachments) >= MAX_ATTACHMENTS:
            break
        attachments.append(
            {
                "filename": path.name,
                "content_type": ctype,
                "content_b64": base64.b64encode(data).decode("ascii"),
            }
        )
    return {
        "message_id": src["message_id"],
        "from": src["from_addr"],
        "subject": src.get("subject", ""),
        "body_text": src.get("body_text", ""),
        "received_at": src.get("received_at"),
        "attachments": attachments,
    }


def encode_body(payload: dict[str, Any]) -> bytes:
    """Compact JSON, UTF-8: the exact bytes that are signed and sent."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign(body: bytes, secret: str) -> str:
    """``X-InvoiceDesk-Signature`` value: ``sha256=<hex HMAC-SHA256 of the raw body>``."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    ap.add_argument("--email", type=Path, default=DEFAULT_EMAIL, help="e-mail fixture JSON")
    ap.add_argument(
        "--api",
        default=os.environ.get("API_BASE_URL", "http://localhost:8000"),
        help="API base URL (without /api/v1)",
    )
    ap.add_argument("--dry-run", action="store_true", help="build and sign, but do not send")
    args = ap.parse_args(argv)

    secret = os.environ.get("INGEST_WEBHOOK_SECRET", "")
    if not secret:
        print("INGEST_WEBHOOK_SECRET is not set", file=sys.stderr)
        return 2
    payload = build_payload(args.email)
    body = encode_body(payload)
    signature = sign(body, secret)
    summary = {
        "message_id": payload["message_id"],
        "attachments": [a["filename"] for a in payload["attachments"]],
        "bytes": len(body),
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **summary}))
        return 0

    import httpx

    resp = httpx.post(
        args.api.rstrip("/") + "/api/v1/ingest/email",
        content=body,
        headers={"Content-Type": "application/json", "X-InvoiceDesk-Signature": signature},
        timeout=60.0,
    )
    print(json.dumps({"status": resp.status_code, **summary}))
    if resp.status_code >= 400:
        print(resp.text[:500], file=sys.stderr)
        return 1
    print(resp.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
