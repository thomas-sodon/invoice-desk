"""Fake and record/replay LLM clients (no network, deterministic).

* :class:`FakeLLM` answers from a responder callable (or a per-role canned dict) and reports
  synthetic usage so cost accounting paths are exercised.
* :class:`ReplayLLM` answers from JSON recordings keyed by :meth:`LLMRequest.replay_key`.
* :class:`RecordingLLM` wraps a real client and writes those recordings (live mode only).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from invoicedesk.config.models import cost_usd, settings_for
from invoicedesk.contracts.llm import LLMClient, LLMRequest, LLMResponse, TextBlock, Usage

__all__ = ["FakeLLM", "RecordingLLM", "ReplayLLM", "ReplayMiss", "estimate_usage"]

Responder = Callable[[LLMRequest], dict[str, Any] | LLMResponse | None]


def estimate_usage(request: LLMRequest, output: dict[str, Any] | None) -> Usage:
    """Rough token counts: ~4 chars/token for text, 2,700 tokens per image (DESIGN §7.1)."""
    sys_chars = sum(len(b.text) for b in request.system)
    user_chars = sum(len(b.text) for b in request.user if isinstance(b, TextBlock))
    images = sum(1 for b in request.user if not isinstance(b, TextBlock))
    cached = any(b.cache for b in request.system)
    out_chars = len(json.dumps(output)) if output is not None else 0
    return Usage(
        input_tokens=user_chars // 4 + images * 2700 + (0 if cached else sys_chars // 4),
        output_tokens=max(1, out_chars // 4),
        cache_read_input_tokens=sys_chars // 4 if cached else 0,
    )


def _response(
    request: LLMRequest, parsed: dict[str, Any] | None, stop_reason: str = "end_turn"
) -> LLMResponse:
    rs = settings_for(request.role)
    usage = estimate_usage(request, parsed)
    return LLMResponse(
        role=request.role,
        model_id=rs.model,
        stop_reason=stop_reason,
        parsed=parsed,
        usage=usage,
        cost_usd=cost_usd(rs.model, **usage.model_dump(), batch=rs.batch),
        latency_ms=0,
        request_id=f"fake-{request.replay_key()[:12]}",
    )


class FakeLLM:
    """In-process fake. ``responder`` may return a dict (parsed output), a full LLMResponse,
    or ``None`` to signal a refusal."""

    def __init__(
        self,
        responder: Responder | None = None,
        *,
        by_role: dict[str, dict[str, Any]] | None = None,
        refuse_roles: set[str] | None = None,
    ) -> None:
        self._responder = responder
        self._by_role = by_role or {}
        self._refuse = refuse_roles or set()
        self.calls: list[LLMRequest] = []
        self.uploads: dict[str, bytes] = {}

    def upload_file(self, data: bytes, *, filename: str, media_type: str) -> str:
        file_id = "file_fake_" + hashlib.sha256(data).hexdigest()[:24]
        self.uploads[file_id] = data
        return file_id

    def parse(self, request: LLMRequest, output_model: type[BaseModel]) -> LLMResponse:
        self.calls.append(request)
        if request.role in self._refuse:
            return _response(request, None, "refusal")
        out: dict[str, Any] | LLMResponse | None
        if self._responder is not None:
            out = self._responder(request)
        elif request.role in self._by_role:
            out = self._by_role[request.role]
        else:
            raise KeyError(f"FakeLLM has no response for role {request.role!r}")
        if isinstance(out, LLMResponse):
            return out
        if out is None:
            return _response(request, None, "refusal")
        validated = output_model.model_validate(out).model_dump(mode="json")
        return _response(request, validated)


class ReplayMiss(KeyError):
    """No recording exists for this request (offline mode cannot call the API)."""


class ReplayLLM:
    """Serves recordings from ``<dir>/<replay_key>.json`` (written by :class:`RecordingLLM`)."""

    def __init__(self, directory: Path, *, fallback: LLMClient | None = None) -> None:
        self.directory = Path(directory)
        self.fallback = fallback

    def upload_file(self, data: bytes, *, filename: str, media_type: str) -> str:
        return "file_replay_" + hashlib.sha256(data).hexdigest()[:24]

    def parse(self, request: LLMRequest, output_model: type[BaseModel]) -> LLMResponse:
        path = self.directory / f"{request.replay_key()}.json"
        if not path.exists():
            if self.fallback is not None:
                return self.fallback.parse(request, output_model)
            raise ReplayMiss(str(path))
        rec = json.loads(path.read_text(encoding="utf-8"))
        resp = LLMResponse.model_validate(rec["response"])
        if resp.parsed is not None:
            output_model.model_validate(resp.parsed)
        return resp


class RecordingLLM:
    """Wraps a real client and records every response for later offline replay."""

    def __init__(self, inner: LLMClient, directory: Path) -> None:
        self.inner = inner
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def upload_file(self, data: bytes, *, filename: str, media_type: str) -> str:
        return self.inner.upload_file(data, filename=filename, media_type=media_type)

    def parse(self, request: LLMRequest, output_model: type[BaseModel]) -> LLMResponse:
        resp = self.inner.parse(request, output_model)
        rec = {
            "request": request.model_dump(mode="json", exclude={"metadata"}),
            "response": resp.model_dump(mode="json"),
        }
        (self.directory / f"{request.replay_key()}.json").write_text(
            json.dumps(rec, indent=1, sort_keys=True), encoding="utf-8"
        )
        return resp
