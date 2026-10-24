"""LLM error types that carry the billed response (no ``anthropic`` import here).

A structured-output failure (schema mismatch, truncated JSON at ``max_tokens``, no text block)
is still a billed call. :class:`StructuredOutputError` keeps the :class:`LLMResponse` built
from the raw body (usage, cost, latency, answering model) so callers can record its cost
(CLAUDE.md: record cost and latency on every call). It subclasses ``LLMCallError`` with kind
``"validation"``, so callers that only know the shared contract keep working.
"""

from __future__ import annotations

from invoicedesk.contracts.llm import LLMCallError, LLMResponse

__all__ = ["StructuredOutputError"]


class StructuredOutputError(LLMCallError):
    def __init__(self, message: str, *, response: LLMResponse | None = None) -> None:
        super().__init__("validation", message)
        self.response = response
