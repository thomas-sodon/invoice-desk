"""OpenTelemetry GenAI spans + cost accounting (M20). STUB: owned by the `extraction` component.

Must provide ``llm_span(role, model_id) -> contextmanager`` and ``setup_tracing(settings)``.
Until then, callers may use :func:`noop_span`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def noop_span(name: str, **attributes: Any) -> Iterator[dict[str, Any]]:
    yield dict(attributes)
