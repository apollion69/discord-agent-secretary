"""Stitched handler + backend integration tests.

These tests wire a real `MulticaBackend` into the slash-command handler
layer to verify cross-cutting flows that are tested in isolation elsewhere
but never exercised end-to-end:

  * /task → backend timeout → circuit opens → ephemeral timeout reply
    (first call) → ephemeral circuit-open reply (second call)

The subprocess layer is mocked via `_FakeProc` (imported from the unit
suite's fixtures pattern) so no real `multica` binary is required.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from discord_agent_secretary.backends import IssueRef
from discord_agent_secretary.backends.multica import MulticaBackend
from discord_agent_secretary.discord_client import build_client
from discord_agent_secretary.handlers import register_handlers

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Minimal test doubles (mirrors test_multica.py but local to this module)
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, data: bytes, *, hang: bool = False) -> None:
        self._data = data
        self._hang = hang
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if self._hang:
            import asyncio as _a
            await _a.sleep(30)
        chunk = self._data[self._pos : self._pos + n] if n >= 0 else self._data[self._pos:]
        self._pos += len(chunk)
        return chunk


class _HangingProc:
    """Subprocess stand-in whose stdout hangs — triggers MulticaCliTimeoutError."""

    def __init__(self) -> None:
        self.stdout = _FakeStream(b"", hang=True)
        self.stderr = _FakeStream(b"")
        self.returncode: int | None = 0
        self.killed = False

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


def _make_interaction(*, user_id: int = 1, guild_id: int = 42) -> MagicMock:
    interaction = MagicMock()
    interaction.id = 12345
    interaction.user.id = user_id
    interaction.guild.id = guild_id
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    return interaction


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTimeoutThenCircuitOpen:
    async def test_timeout_then_circuit_open_sequence(self) -> None:
        """Full end-to-end path:

          1st /task → backend.create_issue hangs → MulticaCliTimeoutError →
          circuit threshold=1 → circuit opens → user sees timeout reply.

          2nd /task → circuit already open → CircuitOpenError fast-fail →
          user sees circuit-open reply.

          Assert that the subprocess was only spawned once (circuit prevents
          the second spawn).
        """
        from discord_agent_secretary.backends import CircuitState

        spawn_count = 0
        hanging_proc = _HangingProc()

        async def _fake_spawn(*args: Any, **kwargs: Any) -> _HangingProc:
            nonlocal spawn_count
            spawn_count += 1
            return hanging_proc

        backend = MulticaBackend(
            cli_path="multica",
            timeout=0.05,
            circuit_failure_threshold=1,
            circuit_reset_timeout=120.0,
        )

        client, tree = build_client()
        register_handlers(tree, backend, guild_id=42)

        from discord import Object
        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        with patch(
            "discord_agent_secretary.backends.multica.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            # --- First call: timeout ---
            i1 = _make_interaction(user_id=1, guild_id=42)
            await cmd.callback(i1, title="first task")

            assert spawn_count == 1
            assert hanging_proc.killed is True
            i1.followup.send.assert_awaited_once()
            msg1 = i1.followup.send.call_args.args[0]
            assert "вовремя" in msg1 or "timeout" in msg1.lower()
            assert i1.followup.send.call_args.kwargs.get("ephemeral") is True
            assert backend.circuit.state == CircuitState.OPEN

            # --- Second call: circuit open fast-fail ---
            i2 = _make_interaction(user_id=1, guild_id=42)
            await cmd.callback(i2, title="second task")

        # No new spawn — circuit fast-failed.
        assert spawn_count == 1

        i2.followup.send.assert_awaited_once()
        msg2 = i2.followup.send.call_args.args[0]
        assert "недоступен" in msg2 or "Tracker" in msg2
        assert i2.followup.send.call_args.kwargs.get("ephemeral") is True


class TestRateLimitBlocksBeforeBackend:
    async def test_rate_limit_prevents_second_backend_call(self) -> None:
        """Rate limiter fires before the backend is invoked — the second
        call from the same user must not reach MulticaBackend at all."""
        from discord_agent_secretary.handlers import RateLimiter

        backend = MagicMock()
        backend.create_issue = AsyncMock(return_value=IssueRef(id="X-1", title="t"))

        limiter = RateLimiter(capacity=1, refill_per_sec=0.0)
        client, tree = build_client()
        register_handlers(tree, backend, guild_id=42, rate_limiter=limiter)

        from discord import Object
        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        i1 = _make_interaction(user_id=7, guild_id=42)
        await cmd.callback(i1, title="ok")
        assert backend.create_issue.await_count == 1

        i2 = _make_interaction(user_id=7, guild_id=42)
        await cmd.callback(i2, title="blocked")
        # Rate limiter fired before backend — still only one backend call.
        assert backend.create_issue.await_count == 1
        i2.response.send_message.assert_awaited_once()
        assert i2.response.send_message.call_args.kwargs.get("ephemeral") is True
