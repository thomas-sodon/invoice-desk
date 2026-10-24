"""AnthropicLLMClient against a mocked HTTP transport (no network, no API key).

The SDK uses ``httpx2``, which ``respx`` cannot patch, so tests inject ``httpx2.MockTransport``.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from invoicedesk.config.models import (
    HAIKU,
    OPUS,
    SERVER_FALLBACK_BETA,
    SONNET,
    ModelRole,
    cost_usd,
)
from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk.contracts.llm import (
    FileImageBlock,
    InlineImageBlock,
    LLMCallError,
    LLMRequest,
    SystemBlock,
    TextBlock,
)
from invoicedesk.llm.anthropic_client import (
    FORBIDDEN_REQUEST_KEYS,
    AnthropicLLMClient,
    build_request_kwargs,
)
from invoicedesk.llm.errors import StructuredOutputError
from invoicedesk.testing.builders import make_extraction
from pydantic import BaseModel, ConfigDict


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int


USAGE = {
    "input_tokens": 1200,
    "output_tokens": 300,
    "cache_read_input_tokens": 3000,
    "cache_creation_input_tokens": 0,
}


def _cost(model: str, *, batch: bool = False) -> Decimal:
    return cost_usd(
        model,
        input_tokens=USAGE["input_tokens"],
        output_tokens=USAGE["output_tokens"],
        cache_read_input_tokens=USAGE["cache_read_input_tokens"],
        cache_creation_input_tokens=USAGE["cache_creation_input_tokens"],
        batch=batch,
    )


def _message(model: str, text: str, stop: str = "end_turn", usage: dict | None = None) -> dict:
    return {
        "id": "msg_test_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage or USAGE,
    }


class Recorder:
    def __init__(self, handler: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.requests: list[httpx2.Request] = []
        self._handler = handler

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def last_body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[AnthropicLLMClient, Recorder]:
    rec = Recorder(handler)
    return AnthropicLLMClient(
        api_key="sk-ant-test", max_retries=0, transport=httpx2.MockTransport(rec)
    ), rec


def _echo(text: str = '{"x": 3}') -> Callable[[httpx2.Request], httpx2.Response]:
    def h(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content)
        return httpx2.Response(200, json=_message(body["model"], text))

    return h


def _req(role: ModelRole, **kw: Any) -> LLMRequest:
    return LLMRequest(
        role=role,
        system=(SystemBlock(text="frozen prefix " * 10, cache=True),),
        user=(
            FileImageBlock(file_id="file_abc"),
            TextBlock(text='OCR words:\n[0] "RAPID" (1,2,3,4)'),
            TextBlock(text='<email_body untrusted="true">\nhello\n</email_body>'),
        ),
        output_schema="tests.Out",
        **kw,
    )


# ---------------------------------------------------------------- request shape per role
@pytest.mark.parametrize(
    ("role", "model", "effort", "thinking", "beta"),
    [
        (ModelRole.EXTRACT_T1, HAIKU, "low", {"type": "disabled"}, False),
        (ModelRole.INJECTION_CLASSIFIER, HAIKU, "low", {"type": "disabled"}, False),
        (ModelRole.WORDING, HAIKU, "low", {"type": "disabled"}, False),
        (ModelRole.EXTRACT_T2, SONNET, "low", {"type": "adaptive"}, True),
        (ModelRole.ADJUDICATE_T3, OPUS, "medium", None, True),
    ],
)
def test_request_body_per_role(
    role: ModelRole, model: str, effort: str, thinking: dict | None, beta: bool
) -> None:
    client, rec = _client(_echo())
    resp = client.parse(_req(role), Out)
    assert resp.parsed == {"x": 3}
    body = rec.last_body
    req = rec.requests[-1]

    assert body["model"] == model
    assert body["output_config"]["effort"] == effort
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["properties"]["x"]["type"] == "integer"
    if thinking is None:
        assert "thinking" not in body  # Opus 5.5 cannot disable thinking; effort controls it
    else:
        assert body["thinking"] == thinking
    if beta:
        assert body["fallbacks"] == "default"
        assert SERVER_FALLBACK_BETA in req.headers["anthropic-beta"]
    else:
        assert "fallbacks" not in body
        assert "anthropic-beta" not in req.headers
    # never: sampling params, tools, tool_choice, prefill
    assert not FORBIDDEN_REQUEST_KEYS & body.keys()
    assert [m["role"] for m in body["messages"]] == ["user"]
    # system prefix carries the cache breakpoint
    assert body["system"] == [
        {"type": "text", "text": "frozen prefix " * 10, "cache_control": {"type": "ephemeral"}}
    ]
    # images first, via Files API file_id; then OCR words; then the email body
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "image", "source": {"type": "file", "file_id": "file_abc"}}
    assert content[1]["text"].startswith("OCR words:")
    assert content[2]["text"].startswith('<email_body untrusted="true">')


def test_max_tokens_override_and_inline_image() -> None:
    req = LLMRequest(
        role=ModelRole.EXTRACT_T1,
        system=(SystemBlock(text="s"),),
        user=(InlineImageBlock(media_type="image/png", data_b64="AAAA"), TextBlock(text="t")),
        output_schema="x",
        max_tokens=77,
    )
    kw = build_request_kwargs(req, Out)
    assert kw["max_tokens"] == 77
    assert kw["system"] == [{"type": "text", "text": "s"}]  # cache=False -> no breakpoint
    assert kw["messages"][0]["content"][0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
    }
    assert kw["output_format"] is Out


def test_full_extraction_schema_round_trip() -> None:
    payload = make_extraction().model_dump_json()
    client, rec = _client(_echo(payload))
    resp = client.parse(_req(ModelRole.EXTRACT_T1), InvoiceExtraction)
    assert resp.parsed is not None
    assert InvoiceExtraction.model_validate(resp.parsed) == make_extraction()
    schema = rec.last_body["output_config"]["format"]["schema"]
    assert "vendor_name" in schema["properties"]


# ---------------------------------------------------------------- cost, usage, latency
def test_cost_and_usage_recorded() -> None:
    client, _ = _client(_echo())
    resp = client.parse(_req(ModelRole.EXTRACT_T2), Out)
    assert resp.usage.cache_read_input_tokens == 3000
    assert resp.cost_usd == _cost(SONNET)
    assert resp.cost_usd > Decimal(0)
    assert resp.latency_ms >= 0
    assert resp.request_id is not None


def test_server_side_fallback_model_is_priced_as_answered() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_message(OPUS, '{"x": 1}'))

    client, _ = _client(h)
    resp = client.parse(_req(ModelRole.EXTRACT_T2), Out)
    assert resp.model_id == OPUS
    assert resp.cost_usd == _cost(OPUS)


def test_unknown_answering_model_priced_as_requested() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_message("claude-future-9", '{"x": 1}'))

    client, _ = _client(h)
    resp = client.parse(_req(ModelRole.EXTRACT_T2), Out)
    assert resp.cost_usd == _cost(SONNET)


# ---------------------------------------------------------------- refusals
def test_refusal_with_non_json_text_is_reported_as_refusal() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_message(HAIKU, "I can't help with that.", "refusal"))

    client, _ = _client(h)
    resp = client.parse(_req(ModelRole.EXTRACT_T1), Out)
    assert resp.refused
    assert resp.parsed is None
    assert resp.cost_usd == _cost(HAIKU)


def test_refusal_checked_before_content_even_if_json_parses() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_message(HAIKU, '{"x": 5}', "refusal"))

    client, _ = _client(h)
    resp = client.parse(_req(ModelRole.EXTRACT_T1), Out)
    assert resp.refused
    assert resp.parsed is None


def test_refusal_with_empty_content() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_message(SONNET, "", "refusal"))

    client, _ = _client(h)
    resp = client.parse(_req(ModelRole.EXTRACT_T2), Out)
    assert resp.refused


# ---------------------------------------------------------------- errors
def test_invalid_structured_output_is_validation_error() -> None:
    client, _ = _client(_echo('{"x": "Acct 000123456789"}'))
    with pytest.raises(LLMCallError) as ei:
        client.parse(_req(ModelRole.EXTRACT_T1), Out)
    assert ei.value.kind == "validation"
    # the billed call keeps its usage and cost
    assert isinstance(ei.value, StructuredOutputError)
    assert ei.value.response is not None
    assert ei.value.response.cost_usd == _cost(HAIKU)
    assert ei.value.response.parsed is None
    # the message names the failing location but never quotes the model's input values
    assert "x" in str(ei.value)
    assert "000123456789" not in str(ei.value)
    assert ei.value.__cause__ is None


def test_truncated_output_at_max_tokens_keeps_cost() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_message(SONNET, '{"x": 1', stop="max_tokens"))

    client, _ = _client(h)
    with pytest.raises(StructuredOutputError) as ei:
        client.parse(_req(ModelRole.EXTRACT_T2), Out)
    assert ei.value.response is not None
    assert ei.value.response.stop_reason == "max_tokens"
    assert ei.value.response.cost_usd == _cost(SONNET)


def test_end_turn_without_text_is_validation_error() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_message(HAIKU, ""))

    client, _ = _client(h)
    with pytest.raises(LLMCallError) as ei:
        client.parse(_req(ModelRole.EXTRACT_T1), Out)
    assert ei.value.kind == "validation"
    assert isinstance(ei.value, StructuredOutputError)
    assert ei.value.response is not None
    assert ei.value.response.cost_usd == _cost(HAIKU)


def _status(code: int, etype: str) -> Callable[[httpx2.Request], httpx2.Response]:
    def h(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            code, json={"type": "error", "error": {"type": etype, "message": "boom"}}
        )

    return h


@pytest.mark.parametrize(
    ("code", "etype", "kind"),
    [
        (429, "rate_limit_error", "rate_limit"),
        (400, "invalid_request_error", "status"),
        (500, "api_error", "status"),
        (529, "overloaded_error", "status"),
    ],
)
def test_status_errors_map_to_kinds(code: int, etype: str, kind: str) -> None:
    client, _ = _client(_status(code, etype))
    with pytest.raises(LLMCallError) as ei:
        client.parse(_req(ModelRole.EXTRACT_T1), Out)
    assert ei.value.kind == kind
    assert ei.value.status_code == code


def test_connection_and_timeout_errors() -> None:
    def conn(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=req)

    def slow(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=req)

    client, _ = _client(conn)
    with pytest.raises(LLMCallError) as ei:
        client.parse(_req(ModelRole.EXTRACT_T1), Out)
    assert ei.value.kind == "connection"

    client, _ = _client(slow)
    with pytest.raises(LLMCallError) as ei:
        client.parse(_req(ModelRole.EXTRACT_T1), Out)
    assert ei.value.kind == "timeout"


# ---------------------------------------------------------------- Files API
def test_upload_file_uses_ga_files_api() -> None:
    def h(req: httpx2.Request) -> httpx2.Response:
        assert req.url.path == "/v1/files"
        return httpx2.Response(
            200,
            json={
                "id": "file_011ABC",
                "type": "file",
                "filename": "page.png",
                "mime_type": "image/png",
                "size_bytes": 4,
                "created_at": "2026-10-24T00:00:00Z",
            },
        )

    client, rec = _client(h)
    fid = client.upload_file(b"\x89PNG", filename="page.png", media_type="image/png")
    assert fid == "file_011ABC"
    assert "anthropic-beta" not in rec.requests[-1].headers  # GA: no beta header
    assert b"image/png" in rec.requests[-1].content


def test_upload_error_maps() -> None:
    client, _ = _client(_status(429, "rate_limit_error"))
    with pytest.raises(LLMCallError) as ei:
        client.upload_file(b"x", filename="a.png", media_type="image/png")
    assert ei.value.kind == "rate_limit"


# ---------------------------------------------------------------- concurrency config
def test_per_tier_semaphores() -> None:
    client = AnthropicLLMClient(
        api_key="k", max_concurrency={"opus": 1}, transport=httpx2.MockTransport(_echo())
    )
    assert client._sems["opus"]._value == 1  # type: ignore[attr-defined]
    assert client._sems["haiku"]._value == 8  # type: ignore[attr-defined]


def test_per_tier_semaphore_limits_in_flight_calls() -> None:
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}
    echo = _echo()

    def slow(req: httpx2.Request) -> httpx2.Response:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return echo(req)

    client = AnthropicLLMClient(
        api_key="k",
        max_retries=0,
        max_concurrency={"opus": 2},
        transport=httpx2.MockTransport(slow),
    )
    threads = [
        threading.Thread(target=client.parse, args=(_req(ModelRole.ADJUDICATE_T3), Out))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 2


# ---------------------------------------------------------------- batch (root cause)
def test_batch_submit_and_collect() -> None:
    state: dict[str, Any] = {}

    def h(req: httpx2.Request) -> httpx2.Response:
        path = req.url.path
        if req.method == "POST" and path == "/v1/messages/batches":
            state["body"] = json.loads(req.content)
            state["headers"] = dict(req.headers)
            return httpx2.Response(200, json=_batch("in_progress", None))
        if req.method == "GET" and path == "/v1/messages/batches/msgbatch_1":
            return httpx2.Response(
                200,
                json=_batch(
                    "ended", "https://api.anthropic.com/v1/messages/batches/msgbatch_1/results"
                ),
            )
        if path.endswith("/results"):
            lines = [
                {
                    "custom_id": "ex-1",
                    "result": {"type": "succeeded", "message": _message(OPUS, '{"x": 9}')},
                },
                {
                    "custom_id": "ex-2",
                    "result": {
                        "type": "errored",
                        "error": {"type": "error", "error": {"type": "api_error", "message": "x"}},
                    },
                },
                {
                    "custom_id": "ex-3",
                    "result": {
                        "type": "succeeded",
                        "message": _message(OPUS, '{"x": "Acct 000123456789"}'),
                    },
                },
            ]
            return httpx2.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
        raise AssertionError(f"unexpected {req.method} {path}")

    client, _ = _client(h)
    batch_id = client.submit_batch([("ex-1", _req(ModelRole.ROOT_CAUSE))], Out)
    assert batch_id == "msgbatch_1"
    params = state["body"]["requests"][0]["params"]
    assert params["model"] == OPUS
    assert params["output_config"]["effort"] == "high"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["fallbacks"] == "default"
    assert not FORBIDDEN_REQUEST_KEYS & params.keys()
    assert SERVER_FALLBACK_BETA in state["headers"]["anthropic-beta"]

    results = client.collect_batch("msgbatch_1", ModelRole.ROOT_CAUSE, Out)
    ok = results["ex-1"]
    assert not isinstance(ok, LLMCallError)
    assert ok.parsed == {"x": 9}
    assert ok.cost_usd == _cost(OPUS, batch=True)
    assert isinstance(results["ex-2"], LLMCallError)
    bad = results["ex-3"]
    assert isinstance(bad, StructuredOutputError)
    assert bad.response is not None
    assert bad.response.cost_usd == _cost(OPUS, batch=True)
    assert "000123456789" not in str(bad)


def test_batch_validation() -> None:
    client, _ = _client(_echo())
    with pytest.raises(ValueError, match="empty"):
        client.submit_batch([], Out)
    with pytest.raises(ValueError, match="one role"):
        client.submit_batch(
            [("a", _req(ModelRole.ROOT_CAUSE)), ("b", _req(ModelRole.EXTRACT_T1))], Out
        )


def _batch(status: str, results_url: str | None) -> dict[str, Any]:
    return {
        "id": "msgbatch_1",
        "type": "message_batch",
        "processing_status": status,
        "request_counts": {
            "processing": 0,
            "succeeded": 1,
            "errored": 1,
            "canceled": 0,
            "expired": 0,
        },
        "created_at": "2026-10-24T00:00:00Z",
        "expires_at": "2026-10-25T00:00:00Z",
        "ended_at": None,
        "archived_at": None,
        "cancel_initiated_at": None,
        "results_url": results_url,
    }


@pytest.mark.live
def test_live_haiku_smoke() -> None:  # pragma: no cover - never in CI
    import os

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("no ANTHROPIC_API_KEY")
    client = AnthropicLLMClient(api_key=key)
    resp = client.parse(
        LLMRequest(
            role=ModelRole.WORDING,
            system=(SystemBlock(text="Return x=1."),),
            user=(TextBlock(text="x?"),),
            output_schema="tests.Out",
        ),
        Out,
    )
    assert resp.parsed == {"x": 1}
