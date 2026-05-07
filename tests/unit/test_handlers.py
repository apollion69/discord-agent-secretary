"""Unit tests for discord_agent_secretary.handlers.

Slash-command registration + error mapping. Handlers depend on the abstract
`IssueBackend` protocol — tests inject a `MagicMock` (duck-typed) or raise
the abstract `BackendError` family directly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from discord_agent_secretary.backends import (
    BackendCallError,
    BackendTimeoutError,
    IssueRef,
)
from discord_agent_secretary.discord_client import build_client
from discord_agent_secretary.handlers import (
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
    register_handlers,
)

pytestmark = pytest.mark.unit


class TestRegisterHandlers:
    def test_registers_three_commands(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        register_handlers(tree, backend, guild_id=42)
        from discord import Object
        registered = {c.name for c in tree.get_commands(guild=Object(id=42))}
        assert registered == {"task", "status", "assign"}

    def test_registers_global_when_no_guild_id(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        register_handlers(tree, backend, guild_id=None)
        registered = {c.name for c in tree.get_commands()}
        assert registered == {"task", "status", "assign"}


class TestAllowedValues:
    def test_statuses_cover_canonical_set(self) -> None:
        assert set(ALLOWED_STATUSES) == {
            "todo", "in_progress", "in_review", "done", "blocked",
        }

    def test_priorities_cover_canonical_set(self) -> None:
        assert set(ALLOWED_PRIORITIES) == {
            "low", "medium", "high", "urgent",
        }


class TestSafeInvoke:
    """Verify _safe_invoke maps abstract backend errors to sanitized replies."""

    async def test_timeout_message(self) -> None:
        from discord_agent_secretary.handlers import _safe_invoke
        interaction = MagicMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        async def boom() -> None:
            raise BackendTimeoutError("timed out")

        result = await _safe_invoke(interaction, boom(), label="test")
        assert result is None
        interaction.followup.send.assert_awaited_once()
        msg = interaction.followup.send.call_args.args[0]
        assert "вовремя" in msg or "timeout" in msg.lower()

    async def test_call_error_does_not_leak_stderr(self) -> None:
        from discord_agent_secretary.handlers import _safe_invoke
        interaction = MagicMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()

        async def boom() -> None:
            raise BackendCallError("SENSITIVE PATH /etc/secrets")

        result = await _safe_invoke(interaction, boom(), label="test")
        assert result is None
        msg = interaction.followup.send.call_args.args[0]
        assert "SENSITIVE" not in msg
        assert "/etc/secrets" not in msg

    async def test_success_returns_value(self) -> None:
        from discord_agent_secretary.handlers import _safe_invoke
        interaction = MagicMock()
        interaction.followup.send = AsyncMock()

        ref = IssueRef(id="abc", title="hello")

        async def ok() -> IssueRef:
            return ref

        result = await _safe_invoke(interaction, ok(), label="test")
        assert result is ref
        interaction.followup.send.assert_not_called()
