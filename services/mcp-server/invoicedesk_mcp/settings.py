"""MCP server settings (``.env``).

The server is an OAuth *resource server* only (DESIGN §4.4, §5.5): tokens come from a hosted
authorization server that supports CIMD client registration (e.g. WorkOS AuthKit). Nothing here
issues tokens. The only secret the server holds is ``AGENT_JWT_SECRET``, shared with the API, which
it uses to mint short-lived service tokens for the agent facade (docs/api-contract.md).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["McpSettings", "get_mcp_settings"]


#: HS256 needs a key at least as long as the hash output (RFC 7518 §3.2).
MIN_AGENT_SECRET_LEN = 32


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    mode: Literal["offline", "live"] = Field("offline", alias="INVOICEDESK_MODE")

    # -- this resource server (RFC 9728 ``resource``, RFC 8707 audience)
    #: Canonical URL of the MCP endpoint. Tokens must name it in ``aud`` (or ``resource``).
    resource_url: str = Field("http://localhost:8100/mcp", alias="MCP_RESOURCE_URL")
    resource_name: str = Field("InvoiceDesk", alias="MCP_RESOURCE_NAME")
    resource_documentation: str | None = Field(None, alias="MCP_RESOURCE_DOCUMENTATION")
    host: str = Field("127.0.0.1", alias="MCP_HOST")
    port: int = Field(8100, alias="MCP_PORT", ge=1, le=65535)
    #: Extra ``Host`` header values accepted besides the resource URL's (DNS-rebinding guard).
    allowed_hosts: str = Field("", alias="MCP_ALLOWED_HOSTS")
    #: Browser ``Origin`` values accepted besides the resource URL's own origin.
    allowed_origins: str = Field("", alias="MCP_ALLOWED_ORIGINS")
    #: ``false`` (default) lets a long ``extract_invoice`` switch to an SSE stream with
    #: keep-alive pings; ``true`` always answers with one JSON body.
    json_response: bool = Field(False, alias="MCP_JSON_RESPONSE")

    # -- hosted authorization server (config only; no hand-rolled OAuth)
    #: Issuer of access tokens, e.g. ``https://<tenant>.authkit.app``. Advertised in the PRM
    #: document and required as the token ``iss``.
    auth_issuer: str | None = Field(None, alias="MCP_AUTH_ISSUER")
    #: JWKS endpoint. Defaults to ``<issuer>/oauth2/jwks`` (the AuthKit path).
    auth_jwks_url: str | None = Field(None, alias="MCP_AUTH_JWKS_URL")
    #: Offline mode only: read the JWKS from a local file (see ``invoicedesk_mcp.devkeys``).
    auth_jwks_file: Path | None = Field(None, alias="MCP_AUTH_JWKS_FILE")
    auth_algorithms: str = Field("RS256 ES256 PS256 EdDSA", alias="MCP_AUTH_ALGORITHMS")
    jwks_cache_ttl_s: float = Field(600.0, alias="MCP_JWKS_CACHE_TTL_S", gt=0)
    #: Clock skew allowed on ``exp``/``nbf``/``iat``.
    auth_leeway_s: int = Field(30, alias="MCP_AUTH_LEEWAY_S", ge=0, le=300)

    # -- the API agent facade
    api_base_url: str = Field("http://localhost:8000/api/v1", alias="MCP_API_BASE_URL")
    agent_jwt_secret: SecretStr | None = Field(None, alias="AGENT_JWT_SECRET")
    agent_jwt_audience: str = Field("invoicedesk-api", alias="AGENT_JWT_AUDIENCE")
    service_token_ttl_s: int = Field(120, alias="MCP_SERVICE_TOKEN_TTL_S", ge=10, le=900)
    #: The API runs extraction synchronously for up to 60 s; leave headroom.
    api_timeout_s: float = Field(90.0, alias="MCP_API_TIMEOUT_S", gt=0)

    # -- protocol behaviour
    tools_list_ttl_ms: int = Field(300_000, alias="MCP_TOOLS_LIST_TTL_MS", ge=0)
    #: Keys that seal the MRTR ``requestState`` (comma-separated; first seals, all unseal).
    #: Unset: a per-process key, which is fine for one replica. Set it when running several.
    request_state_keys: SecretStr | None = Field(None, alias="MCP_REQUEST_STATE_KEYS")
    request_state_ttl_s: float = Field(900.0, alias="MCP_REQUEST_STATE_TTL_S", gt=0)

    @field_validator("agent_jwt_secret", mode="before")
    @classmethod
    def _agent_secret(cls, v: object) -> object:
        """Blank means "not configured" (an empty line copied from .env.example must never
        become an HS256 key); a configured secret must be at least 32 characters."""
        raw = v.get_secret_value() if isinstance(v, SecretStr) else v
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if isinstance(raw, str) and len(raw) < MIN_AGENT_SECRET_LEN:
            raise ValueError(f"AGENT_JWT_SECRET must be at least {MIN_AGENT_SECRET_LEN} characters")
        return v

    @field_validator("resource_url")
    @classmethod
    def _resource_url(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("MCP_RESOURCE_URL must be an absolute http(s) URL")
        if parts.query or parts.fragment:
            raise ValueError("MCP_RESOURCE_URL must not carry a query or fragment (RFC 8707)")
        return v.rstrip("/")

    @property
    def mcp_path(self) -> str:
        # The endpoint is served exactly at the resource URL, so the RFC 8707 audience and the
        # PRM ``resource`` always name the URL clients connect to.
        return urlsplit(self.resource_url).path or "/"

    @property
    def resource_netloc(self) -> str:
        return urlsplit(self.resource_url).netloc

    @property
    def jwks_url(self) -> str | None:
        if self.auth_jwks_url:
            return self.auth_jwks_url
        if self.auth_issuer:
            return self.auth_issuer.rstrip("/") + "/oauth2/jwks"
        return None

    @property
    def algorithms(self) -> list[str]:
        # Asymmetric only: an HS* key built from a public JWKS invites algorithm confusion.
        return [a for a in self.auth_algorithms.split() if a[:2].upper() not in ("HS", "NO")]

    @property
    def extra_hosts(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def origins(self) -> list[str]:
        parts = urlsplit(self.resource_url)
        extra = [o.strip() for o in self.allowed_origins.split(",") if o.strip()]
        return [f"{parts.scheme}://{parts.netloc}", *extra]


@lru_cache(maxsize=1)
def get_mcp_settings() -> McpSettings:
    return McpSettings()  # pyright: ignore[reportCallIssue]
