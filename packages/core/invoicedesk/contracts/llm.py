"""Provider-neutral LLM client contract.

Every Claude call in the codebase goes through :class:`LLMClient`. The real implementation
(``invoicedesk.llm.anthropic_client``, owned by the extraction component) is the ONLY module
allowed to import ``anthropic`` (enforced by ruff TID251). Tests and offline demo mode use
:class:`invoicedesk.fakes.llm.FakeLLM` / :class:`invoicedesk.fakes.llm.ReplayLLM`.

Requests never carry tools (the extractor is tool-less, invariant 2) and never carry sampling
parameters. The caller names a :class:`~invoicedesk.config.models.ModelRole`; the client
resolves model ID, effort, thinking and fallbacks from ``config.models``.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from invoicedesk.config.models import ModelRole

__all__ = [
    "ContentBlock",
    "FileImageBlock",
    "InlineImageBlock",
    "LLMCallError",
    "LLMClient",
    "LLMRefusal",
    "LLMRequest",
    "LLMResponse",
    "SystemBlock",
    "TextBlock",
    "Usage",
]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SystemBlock(_M):
    """A frozen system-prompt segment. ``cache=True`` puts a cache breakpoint after it.
    Never put timestamps or IDs here (it would break the cached prefix)."""

    text: str
    cache: bool = False


class TextBlock(_M):
    type: Literal["text"] = "text"
    text: str
    cache: bool = False


class FileImageBlock(_M):
    """An image previously uploaded via :meth:`LLMClient.upload_file` (Files API, GA)."""

    type: Literal["file_image"] = "file_image"
    file_id: str


class InlineImageBlock(_M):
    """Base64 image fallback (offline/replay or when the Files API is not used)."""

    type: Literal["inline_image"] = "inline_image"
    media_type: Literal["image/png", "image/jpeg"]
    data_b64: str


ContentBlock = TextBlock | FileImageBlock | InlineImageBlock


class LLMRequest(_M):
    role: ModelRole
    system: tuple[SystemBlock, ...]
    #: Single user turn. Images first, then OCR words, then the untrusted email body.
    user: tuple[ContentBlock, ...]
    #: Pydantic model passed as ``output_format`` to ``messages.parse()``.
    output_schema: str = Field(description="Dotted path of the output model, for replay keys")
    max_tokens: int | None = None  # override RoleSettings.max_tokens
    metadata: dict[str, str] = Field(
        default_factory=dict
    )  # trace/document ids (not sent in prompt)

    def replay_key(self) -> str:
        """Stable key for record/replay: role + schema + prompt content (not metadata)."""
        payload = self.model_dump(mode="json", exclude={"metadata", "max_tokens"})
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


class Usage(_M):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class LLMResponse(_M):
    role: ModelRole
    model_id: str  # the model that actually answered (server-side fallback may change it)
    stop_reason: str  # "end_turn" | "max_tokens" | "refusal" | ...
    #: Parsed structured output as JSON-able dict (``None`` on refusal / parse failure).
    parsed: dict[str, Any] | None
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    request_id: str | None = None
    trace_id: str | None = None

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


class LLMRefusal(Exception):
    """Raised by helpers when ``stop_reason == "refusal"`` and the caller wants an exception."""

    def __init__(self, response: LLMResponse) -> None:
        super().__init__(f"{response.model_id} refused ({response.role})")
        self.response = response


class LLMCallError(Exception):
    """Wraps a provider error after the client's own retries. ``kind`` mirrors the SDK
    exception classes so callers can branch without importing ``anthropic``."""

    def __init__(
        self,
        kind: Literal["rate_limit", "status", "connection", "timeout", "validation"],
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.status_code = status_code


@runtime_checkable
class LLMClient(Protocol):
    def upload_file(self, data: bytes, *, filename: str, media_type: str) -> str:
        """Upload once (Files API) and return a ``file_id`` reusable across tiers."""
        ...

    def parse(self, request: LLMRequest, output_model: type[BaseModel]) -> LLMResponse:
        """Structured-output call (``messages.parse``). Must check refusal before content.
        Must record cost via ``config.models.cost_usd``. Never sends sampling params/tools."""
        ...
