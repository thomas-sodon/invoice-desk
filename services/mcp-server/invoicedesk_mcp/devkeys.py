"""Offline development keys for the MCP server (``INVOICEDESK_MODE=offline`` only).

In production the hosted authorization server (e.g. WorkOS AuthKit, with CIMD client
registration) issues access tokens and this server only verifies them. To try the server
locally without one (MCP Inspector, curl), this tool writes an RSA key pair under ``var/mcp-dev``
(git-ignored), a JWKS file the server reads via ``MCP_AUTH_JWKS_FILE``, and prints an access
token for the configured resource. It is a key-and-token printer for local testing, not an OAuth
server: there is no authorize, token or registration endpoint, and live mode refuses the file.

    uv run python -m invoicedesk_mcp.devkeys --scopes "invoices:read invoices:write bills:post"
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from invoicedesk_mcp.auth import ALL_SCOPES
from invoicedesk_mcp.settings import McpSettings

__all__ = ["dev_token", "ensure_dev_keys", "main"]

DEV_DIR = Path("var/mcp-dev")
DEV_ISSUER = "http://localhost:8100/dev-issuer"
_KID = "invoicedesk-dev-1"


def ensure_dev_keys(directory: Path = DEV_DIR) -> tuple[rsa.RSAPrivateKey, Path]:
    """Load or create the dev key pair; returns the private key and the JWKS path."""
    directory.mkdir(parents=True, exist_ok=True)
    pem = directory / "private.pem"
    jwks = directory / "jwks.json"
    if pem.exists():
        loaded = serialization.load_pem_private_key(pem.read_bytes(), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise SystemExit(f"{pem} is not an RSA private key")
        key = loaded
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        fd = os.open(pem, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
    pub = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    pub.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    jwks.write_text(json.dumps({"keys": [pub]}, indent=2))
    return key, jwks


def dev_token(
    key: rsa.RSAPrivateKey,
    *,
    issuer: str,
    resource: str,
    scopes: str,
    client_id: str = "invoicedesk-dev-client",
    subject: str = "dev-user",
    ttl_s: int = 3600,
) -> str:
    now = dt.datetime.now(dt.UTC)
    return jwt.encode(
        {
            "iss": issuer,
            "aud": resource,
            "sub": subject,
            "client_id": client_id,
            "scope": scopes,
            "iat": now,
            "exp": now + dt.timedelta(seconds=ttl_s),
            "jti": uuid.uuid4().hex,
        },
        key,
        algorithm="RS256",
        headers={"kid": _KID},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invoicedesk-mcp-devkeys", description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEV_DIR)
    parser.add_argument("--scopes", default=" ".join(ALL_SCOPES))
    parser.add_argument("--client-id", default="invoicedesk-dev-client")
    parser.add_argument("--ttl", type=int, default=3600)
    args = parser.parse_args(argv)
    s = McpSettings()  # pyright: ignore[reportCallIssue]
    if s.mode != "offline":
        print("devkeys is for INVOICEDESK_MODE=offline only.", file=sys.stderr)
        return 2
    issuer = s.auth_issuer or DEV_ISSUER
    key, jwks = ensure_dev_keys(args.dir)
    token = dev_token(
        key,
        issuer=issuer,
        resource=s.resource_url,
        scopes=args.scopes,
        client_id=args.client_id,
        ttl_s=args.ttl,
    )
    print(f"MCP_AUTH_ISSUER={issuer}")
    print(f"MCP_AUTH_JWKS_FILE={jwks}")
    print(f"MCP_RESOURCE_URL={s.resource_url}")
    print(f"# Authorization: Bearer {token}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
