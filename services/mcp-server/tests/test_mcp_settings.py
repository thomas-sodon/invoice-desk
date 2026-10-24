"""MCP server settings: the shared agent secret is validated at the boundary."""

from __future__ import annotations

import pytest


def test_blank_or_short_agent_secret_is_never_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from invoicedesk_mcp.settings import McpSettings
    from pydantic import ValidationError

    monkeypatch.setenv("AGENT_JWT_SECRET", "")
    assert McpSettings().agent_jwt_secret is None  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("AGENT_JWT_SECRET", "short")
    with pytest.raises(ValidationError):
        McpSettings()  # pyright: ignore[reportCallIssue]
