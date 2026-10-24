"""Environment settings (``.env``). Secrets live only here; see ``.env.example``.

``INVOICEDESK_MODE=offline`` (the default) wires every external dependency to its deterministic
fake/replay implementation, so tests and the offline demo never call paid APIs.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mode: Literal["offline", "live"] = Field("offline", alias="INVOICEDESK_MODE")
    tenant: str = Field("maple_row", alias="INVOICEDESK_TENANT")
    database_url: str = Field(
        "postgresql://invoicedesk:invoicedesk@localhost:54322/invoicedesk", alias="DATABASE_URL"
    )
    tenants_dir: Path = Field(REPO_ROOT / "tenants", alias="INVOICEDESK_TENANTS_DIR")
    storage_backend: Literal["local", "memory", "supabase"] = Field(
        "local", alias="STORAGE_BACKEND"
    )
    storage_local_dir: Path = Field(REPO_ROOT / "var" / "storage", alias="STORAGE_LOCAL_DIR")
    replay_dir: Path = Field(REPO_ROOT / "data" / "replay", alias="LLM_REPLAY_DIR")

    anthropic_api_key: SecretStr | None = Field(None, alias="ANTHROPIC_API_KEY")
    supabase_url: str | None = Field(None, alias="SUPABASE_URL")
    supabase_service_role_key: SecretStr | None = Field(None, alias="SUPABASE_SERVICE_ROLE_KEY")
    supabase_jwt_secret: SecretStr | None = Field(None, alias="SUPABASE_JWT_SECRET")

    qbo_client_id: str | None = Field(None, alias="QBO_CLIENT_ID")
    qbo_client_secret: SecretStr | None = Field(None, alias="QBO_CLIENT_SECRET")
    qbo_realm_id: str | None = Field(None, alias="QBO_REALM_ID")
    qbo_refresh_token: SecretStr | None = Field(None, alias="QBO_REFRESH_TOKEN")
    qbo_environment: Literal["sandbox"] = Field("sandbox", alias="QBO_ENVIRONMENT")

    langfuse_host: str | None = Field(None, alias="LANGFUSE_HOST")
    langfuse_public_key: str | None = Field(None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: SecretStr | None = Field(None, alias="LANGFUSE_SECRET_KEY")

    turnstile_secret_key: SecretStr | None = Field(None, alias="TURNSTILE_SECRET_KEY")
    session_signing_secret: SecretStr = Field(
        SecretStr("dev-only-change-me"), alias="SESSION_SIGNING_SECRET"
    )
    ingest_webhook_secret: SecretStr = Field(
        SecretStr("dev-only-change-me"), alias="INGEST_WEBHOOK_SECRET"
    )
    public_daily_spend_cap_usd: Decimal = Field(Decimal("3.00"), alias="PUBLIC_DAILY_SPEND_CAP_USD")
    worker_concurrency: int = Field(8, alias="WORKER_CONCURRENCY")
    api_base_url: str = Field("http://localhost:8000", alias="API_BASE_URL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
