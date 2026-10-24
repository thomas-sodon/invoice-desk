"""The real Claude client: the ONLY module that imports ``anthropic`` (ruff TID251).

Implements :class:`invoicedesk.contracts.llm.LLMClient`:

* ``messages.parse(output_format=Model, output_config={"effort": ...})`` for structured output.
  Sonnet and Opus go through ``client.beta.messages.parse`` with ``fallbacks="default"`` and
  ``betas=[server-side-fallback-2026-07-01]`` (``RoleSettings.uses_beta_endpoint``); Haiku uses
  the GA endpoint with thinking disabled at effort ``low``.
* Never sends ``temperature``/``top_p``/``top_k``, tools, ``tool_choice`` or an assistant
  prefill (all 400s on the 5.5 models, and the extractor is tool-less by design).
* System blocks flagged ``cache=True`` get an ephemeral ``cache_control`` breakpoint.
* Page images are uploaded once through the Files API (GA, ``client.files.upload``) and sent as
  ``{"type": "image", "source": {"type": "file", "file_id": ...}}``.
* ``stop_reason == "refusal"`` is checked before content is read. When a refusal's text is not
  valid JSON the SDK's parser raises; the raw body captured by an httpx response hook lets us
  still report it as a refusal (with its usage) rather than a validation error.
* SDK exceptions map to :class:`LLMCallError` kinds; cost comes from
  ``config.models.cost_usd`` and latency is measured around the call. One semaphore per tier.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import pydantic

from invoicedesk.config.models import PRICES, ModelRole, RoleSettings, Tier, cost_usd, settings_for
from invoicedesk.contracts.llm import (
    ContentBlock,
    FileImageBlock,
    InlineImageBlock,
    LLMCallError,
    LLMRequest,
    LLMResponse,
    TextBlock,
    Usage,
)
from invoicedesk.telemetry import llm_span

__all__ = [
    "DEFAULT_TIER_CONCURRENCY",
    "FORBIDDEN_REQUEST_KEYS",
    "AnthropicLLMClient",
    "build_request_kwargs",
    "to_content_param",
]

#: Per-tier concurrency (worker concurrency is 8; the expensive tiers are throttled harder).
DEFAULT_TIER_CONCURRENCY: dict[Tier, int] = {"haiku": 8, "sonnet": 4, "opus": 2}

#: Keys that must never appear in a request (400s on 5.5 models, or break invariant 2).
FORBIDDEN_REQUEST_KEYS = frozenset({"temperature", "top_p", "top_k", "tools", "tool_choice"})

_CACHE = {"type": "ephemeral"}


def to_content_param(block: ContentBlock) -> dict[str, Any]:
    """Map a provider-neutral content block to an Anthropic content block param."""
    if isinstance(block, TextBlock):
        out: dict[str, Any] = {"type": "text", "text": block.text}
        if block.cache:
            out["cache_control"] = dict(_CACHE)
        return out
    if isinstance(block, FileImageBlock):
        return {"type": "image", "source": {"type": "file", "file_id": block.file_id}}
    if isinstance(block, InlineImageBlock):
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": block.media_type, "data": block.data_b64},
        }
    raise TypeError(f"unsupported content block {type(block).__name__}")


def build_request_kwargs(
    request: LLMRequest, output_model: type[pydantic.BaseModel]
) -> dict[str, Any]:
    """Keyword arguments for ``messages.parse`` / ``beta.messages.parse`` (pure function).

    The single user turn is the whole conversation: no prefill, no tools, no sampling params.
    """
    rs = settings_for(request.role)
    kw = rs.request_kwargs()
    if request.max_tokens is not None:
        kw["max_tokens"] = request.max_tokens
    if not rs.uses_beta_endpoint:
        kw.pop("fallbacks", None)
        kw.pop("betas", None)
    system: list[dict[str, Any]] = []
    for sb in request.system:
        block: dict[str, Any] = {"type": "text", "text": sb.text}
        if sb.cache:
            block["cache_control"] = dict(_CACHE)
        system.append(block)
    if system:
        kw["system"] = system
    kw["messages"] = [{"role": "user", "content": [to_content_param(b) for b in request.user]}]
    kw["output_format"] = output_model
    bad = FORBIDDEN_REQUEST_KEYS & kw.keys()
    if bad:  # pragma: no cover - guarded by RoleSettings; defensive
        raise ValueError(f"forbidden request keys: {sorted(bad)}")
    return kw


class _LastBody(threading.local):
    body: dict[str, Any] | None = None


class AnthropicLLMClient:
    """Thread-safe synchronous client used by the worker (concurrency 8) and the API."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        max_concurrency: Mapping[Tier, int] | None = None,
        max_retries: int = 2,
        timeout_s: float = 120.0,
        base_url: str | None = None,
        transport: httpx2.BaseTransport | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        """``transport`` lets tests inject ``httpx2.MockTransport`` (the SDK uses ``httpx2``,
        so ``respx``, which patches ``httpx``, cannot intercept it)."""
        self._last = _LastBody()
        if client is None:
            http_client = httpx2.Client(
                timeout=timeout_s,
                transport=transport,
                event_hooks={"response": [self._capture_body]},
            )
            client = anthropic.Anthropic(
                api_key=api_key,
                base_url=base_url,
                max_retries=max_retries,  # the SDK retries 429/5xx/connection with backoff
                http_client=http_client,
            )
        self._client = client
        limits: dict[Tier, int] = {**DEFAULT_TIER_CONCURRENCY, **(max_concurrency or {})}
        self._sems: dict[Tier, threading.BoundedSemaphore] = {
            tier: threading.BoundedSemaphore(n) for tier, n in limits.items()
        }

    # ------------------------------------------------------------------ hooks
    def _capture_body(self, response: httpx2.Response) -> None:
        """Keep the last ``/v1/messages`` JSON body on this thread (refusal recovery only)."""
        if not response.request.url.path.endswith("/v1/messages"):
            return
        try:
            response.read()
            body = response.json()
        except (json.JSONDecodeError, httpx2.HTTPError, UnicodeDecodeError):
            body = None
        self._last.body = body if isinstance(body, dict) else None

    # ------------------------------------------------------------------ files
    def upload_file(self, data: bytes, *, filename: str, media_type: str) -> str:
        """Files API (GA, no beta header). Returns the ``file_id`` to reuse across tiers."""
        try:
            meta = self._client.files.upload(file=(filename, data, media_type))
        except anthropic.APIError as exc:
            raise _map_error(exc) from exc
        return meta.id

    # ------------------------------------------------------------------ parse
    def parse(self, request: LLMRequest, output_model: type[pydantic.BaseModel]) -> LLMResponse:
        rs = settings_for(request.role)
        kwargs = build_request_kwargs(request, output_model)
        with (
            self._sems[rs.tier],
            llm_span(
                request.role,
                rs.model,
                **{f"invoicedesk.{k}": v for k, v in request.metadata.items()},
            ) as span,
        ):
            span.set(**{"gen_ai.request.max_tokens": kwargs["max_tokens"]})
            self._last.body = None
            t0 = time.perf_counter()
            try:
                if rs.uses_beta_endpoint:
                    msg: Any = self._client.beta.messages.parse(**kwargs)
                else:
                    msg = self._client.messages.parse(**kwargs)
            except pydantic.ValidationError as exc:
                latency = _ms(t0)
                body = self._last.body
                if body is not None and body.get("stop_reason") == "refusal":
                    resp = self._from_body(request, rs, body, latency, span.trace_id)
                    span.record(resp)
                    return resp
                span.error("validation", "structured output did not match the schema")
                raise LLMCallError("validation", f"{output_model.__name__}: {exc}") from exc
            except anthropic.APIError as exc:
                err = _map_error(exc)
                span.error(err.kind, str(exc))
                raise err from exc
            latency = _ms(t0)
            resp = self._from_message(request, rs, msg, latency, span.trace_id)
            span.record(resp)
            return resp

    # ------------------------------------------------------------------ helpers
    def _from_message(
        self, request: LLMRequest, rs: RoleSettings, msg: Any, latency: int, trace_id: str | None
    ) -> LLMResponse:
        usage = _usage(msg.usage)
        model_id = str(msg.model or rs.model)
        stop = str(msg.stop_reason or "end_turn")
        parsed: dict[str, Any] | None = None
        if stop != "refusal":  # check the refusal BEFORE touching content
            out = msg.parsed_output
            if out is None:
                raise LLMCallError("validation", f"no structured output (stop_reason={stop})")
            parsed = out.model_dump(mode="json")
        return LLMResponse(
            role=request.role,
            model_id=model_id,
            stop_reason=stop,
            parsed=parsed,
            usage=usage,
            cost_usd=_cost(model_id, rs, usage),
            latency_ms=latency,
            request_id=getattr(msg, "_request_id", None) or getattr(msg, "id", None),
            trace_id=trace_id,
        )

    def _from_body(
        self,
        request: LLMRequest,
        rs: RoleSettings,
        body: dict[str, Any],
        latency: int,
        trace_id: str | None,
    ) -> LLMResponse:
        u = body.get("usage") or {}
        usage = Usage(
            input_tokens=int(u.get("input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            cache_read_input_tokens=int(u.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(u.get("cache_creation_input_tokens") or 0),
        )
        model_id = str(body.get("model") or rs.model)
        return LLMResponse(
            role=request.role,
            model_id=model_id,
            stop_reason="refusal",
            parsed=None,
            usage=usage,
            cost_usd=_cost(model_id, rs, usage),
            latency_ms=latency,
            request_id=body.get("id"),
            trace_id=trace_id,
        )

    # ------------------------------------------------------------------ batch (root cause)
    def submit_batch(
        self, items: list[tuple[str, LLMRequest]], output_model: type[pydantic.BaseModel]
    ) -> str:
        """Message Batches API for offline roles (``ROOT_CAUSE``): half price, async."""
        if not items:
            raise ValueError("empty batch")
        roles = {req.role for _, req in items}
        if len(roles) != 1:
            raise ValueError("one role per batch")
        rs = settings_for(next(iter(roles)))
        requests: list[dict[str, Any]] = []
        for custom_id, req in items:
            params = build_request_kwargs(req, output_model)
            params.pop("output_format")
            params.pop("betas", None)
            params["output_config"] = {
                **params["output_config"],
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(output_model.model_json_schema()),
                },
            }
            requests.append({"custom_id": custom_id, "params": params})
        try:
            if rs.uses_beta_endpoint:
                batch = self._client.beta.messages.batches.create(
                    requests=requests,  # pyright: ignore[reportArgumentType]
                    betas=list(rs.betas),  # pyright: ignore[reportArgumentType]
                )
            else:
                batch = self._client.messages.batches.create(
                    requests=requests  # pyright: ignore[reportArgumentType]
                )
        except anthropic.APIError as exc:
            raise _map_error(exc) from exc
        return batch.id

    def collect_batch(
        self, batch_id: str, role: ModelRole, output_model: type[pydantic.BaseModel]
    ) -> dict[str, LLMResponse | LLMCallError]:
        """Results of an ended batch, keyed by ``custom_id`` (batch pricing applied)."""
        rs = settings_for(role)
        out: dict[str, LLMResponse | LLMCallError] = {}
        try:
            results: Any = (
                self._client.beta.messages.batches.results(batch_id)
                if rs.uses_beta_endpoint
                else self._client.messages.batches.results(batch_id)
            )
            for item in results:
                res = item.result
                if res.type != "succeeded":
                    out[item.custom_id] = LLMCallError("status", f"batch item {res.type}")
                    continue
                msg = res.message
                usage = _usage(msg.usage)
                model_id = str(msg.model or rs.model)
                stop = str(msg.stop_reason or "end_turn")
                parsed: dict[str, Any] | None = None
                if stop != "refusal":
                    text = "".join(b.text for b in msg.content if b.type == "text")
                    try:
                        parsed = output_model.model_validate_json(text).model_dump(mode="json")
                    except pydantic.ValidationError as exc:
                        out[item.custom_id] = LLMCallError("validation", str(exc))
                        continue
                out[item.custom_id] = LLMResponse(
                    role=role,
                    model_id=model_id,
                    stop_reason=stop,
                    parsed=parsed,
                    usage=usage,
                    cost_usd=_cost(model_id, rs, usage, batch=True),
                    latency_ms=0,
                    request_id=msg.id,
                )
        except anthropic.APIError as exc:
            raise _map_error(exc) from exc
        except anthropic.AnthropicError as exc:  # e.g. batch has no results_url yet
            raise LLMCallError("status", str(exc)) from exc
        return out


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _usage(u: Any) -> Usage:
    return Usage(
        input_tokens=int(getattr(u, "input_tokens", 0) or 0),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
        cache_read_input_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
        cache_creation_input_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    )


def _cost(model_id: str, rs: RoleSettings, usage: Usage, *, batch: bool = False) -> Decimal:
    """Price by the model that answered (server-side fallback may switch it), else the request's."""
    priced = model_id if model_id in PRICES else rs.model
    return cost_usd(priced, **usage.model_dump(), batch=batch)


def _map_error(exc: anthropic.APIError) -> LLMCallError:
    """Specific SDK classes first: timeout is a subclass of connection, 429 of status."""
    if isinstance(exc, anthropic.RateLimitError):
        return LLMCallError("rate_limit", exc.message, status_code=exc.status_code)
    if isinstance(exc, anthropic.APITimeoutError):
        return LLMCallError("timeout", exc.message)
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMCallError("connection", exc.message)
    if isinstance(exc, anthropic.APIStatusError):
        return LLMCallError("status", exc.message, status_code=exc.status_code)
    return LLMCallError("status", str(exc))
