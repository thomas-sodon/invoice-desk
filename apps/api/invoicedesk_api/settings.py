"""API-only settings (``.env``). Shared settings (mode, DB, keys, spend cap) come from
:class:`invoicedesk.config.settings.Settings`; this class adds what only the gateway reads."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from invoicedesk.config.settings import REPO_ROOT
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["TRYIT_BANNER", "ApiSettings", "get_api_settings"]

#: Shown on the try-it page (US-9): visitors should upload redacted invoices only.
TRYIT_BANNER = (
    "Please upload a redacted invoice: black out bank and account numbers and any personal "
    "details first. Uploads are deleted after 24 hours and are never posted to accounting."
)


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # -- pipeline execution
    #: ``embedded``: the API process runs the pipeline for its uploads on its own job threads
    #: (offline default; one process is enough). ``worker``: uploads are queued as
    #: ``process_document`` jobs for ``invoicedesk-worker``. Demo batches always run embedded,
    #: because they need the demo date and (offline) the gold-backed oracle LLM.
    pipeline_mode: Literal["embedded", "worker"] | None = Field(None, alias="API_PIPELINE_MODE")
    embedded_worker: bool = Field(True, alias="API_EMBEDDED_WORKER")
    embedded_concurrency: int = Field(4, alias="API_EMBEDDED_CONCURRENCY", ge=1, le=16)

    # -- staff + agent auth
    #: HS256 secret for MCP service tokens (``scope``, ``act``); falls back to nothing (401).
    agent_jwt_secret: SecretStr | None = Field(None, alias="AGENT_JWT_SECRET")
    agent_jwt_audience: str = Field("invoicedesk-api", alias="AGENT_JWT_AUDIENCE")
    supabase_jwt_audience: str = Field("authenticated", alias="SUPABASE_JWT_AUDIENCE")

    # -- follow-ups (M15)
    n8n_followup_webhook_url: str | None = Field(None, alias="N8N_FOLLOWUP_WEBHOOK_URL")

    # -- try-it limits (M14); the $/day cap is Settings.public_daily_spend_cap_usd
    tryit_uploads_per_session: int = Field(3, alias="TRYIT_UPLOADS_PER_SESSION", ge=0)
    tryit_uploads_per_ip_per_day: int = Field(10, alias="TRYIT_UPLOADS_PER_IP_PER_DAY", ge=0)
    tryit_sessions_per_ip_per_day: int = Field(5, alias="TRYIT_SESSIONS_PER_IP_PER_DAY", ge=0)
    tryit_demo_runs_per_ip_per_day: int = Field(5, alias="TRYIT_DEMO_RUNS_PER_IP_PER_DAY", ge=0)
    #: Headroom kept under the cap for one in-flight document (about an Opus-only document,
    #: DESIGN §7.3), so the last accepted upload cannot push the day far past the cap.
    tryit_cost_reserve_usd: Decimal = Field(Decimal("0.07"), alias="TRYIT_COST_RESERVE_USD")
    tryit_max_tier: Literal["haiku", "sonnet", "opus"] = Field("sonnet", alias="TRYIT_MAX_TIER")
    tryit_session_ttl_s: int = Field(24 * 3600, alias="TRYIT_SESSION_TTL_S")
    cookie_secure: bool = Field(False, alias="COOKIE_SECURE")
    #: Trust the left-most ``X-Forwarded-For`` hop (set behind Fly.io / a proxy only).
    trust_forwarded_for: bool = Field(False, alias="TRUST_FORWARDED_FOR")

    # -- misc
    report_card_dir: Path = Field(REPO_ROOT / "evals" / "out", alias="REPORT_CARD_DIR")
    sse_keepalive_s: float = Field(15.0, alias="SSE_KEEPALIVE_S", gt=0)
    web_origins: str = Field("http://localhost:3000", alias="WEB_ORIGINS")
    supabase_storage_bucket: str = Field("invoices", alias="SUPABASE_STORAGE_BUCKET")


@lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    return ApiSettings()  # pyright: ignore[reportCallIssue]
