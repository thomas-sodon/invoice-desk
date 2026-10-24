"""Deterministic fakes for every external dependency (tests + offline demo mode)."""

from invoicedesk.fakes.accounting import FakeAccounting
from invoicedesk.fakes.events import InMemoryEventPublisher
from invoicedesk.fakes.llm import FakeLLM, RecordingLLM, ReplayLLM, ReplayMiss
from invoicedesk.fakes.queue import InMemoryJobQueue

__all__ = [
    "FakeAccounting",
    "FakeLLM",
    "InMemoryEventPublisher",
    "InMemoryJobQueue",
    "RecordingLLM",
    "ReplayLLM",
    "ReplayMiss",
]
