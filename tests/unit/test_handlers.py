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
    RateLimiter,
    register_handlers,
)

pytestmark = pytest.mark.unit


def _make_interaction(*, user_id: int = 1, guild_id: int = 2) -> MagicMock:
    interaction = MagicMock()
    interaction.id = 12345
    interaction.user.id = user_id
    interaction.guild.id = guild_id
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


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
    """Verify _safe_invoke maps abstract backend errors to ephemeral replies."""

    async def test_timeout_message_is_ephemeral(self) -> None:
        from discord_agent_secretary.handlers import _safe_invoke
        interaction = _make_interaction()

        async def boom() -> None:
            raise BackendTimeoutError("timed out")

        result = await _safe_invoke(interaction, boom(), label="test")
        assert result is None
        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs.get("ephemeral") is True
        msg = interaction.followup.send.call_args.args[0]
        assert "вовремя" in msg or "timeout" in msg.lower()

    async def test_call_error_does_not_leak_stderr(self) -> None:
        from discord_agent_secretary.handlers import _safe_invoke
        interaction = _make_interaction()

        async def boom() -> None:
            raise BackendCallError("SENSITIVE PATH /etc/secrets")

        result = await _safe_invoke(interaction, boom(), label="test")
        assert result is None
        msg = interaction.followup.send.call_args.args[0]
        assert "SENSITIVE" not in msg
        assert "/etc/secrets" not in msg
        assert interaction.followup.send.call_args.kwargs.get("ephemeral") is True

    async def test_success_returns_value(self) -> None:
        from discord_agent_secretary.handlers import _safe_invoke
        interaction = _make_interaction()

        ref = IssueRef(id="abc", title="hello")

        async def ok() -> IssueRef:
            return ref

        result = await _safe_invoke(interaction, ok(), label="test")
        assert result is ref
        interaction.followup.send.assert_not_called()

    async def test_swallows_followup_http_errors(self) -> None:
        """If Discord rejects the followup, the handler must not crash."""
        from discord import HTTPException

        from discord_agent_secretary.handlers import _safe_invoke
        interaction = _make_interaction()
        # Build a minimal HTTPException that doesn't require a real response.
        interaction.followup.send.side_effect = HTTPException(
            response=MagicMock(status=503, reason="x"),
            message="boom",
        )

        async def fail() -> None:
            raise BackendTimeoutError()

        result = await _safe_invoke(interaction, fail(), label="test")
        assert result is None  # error path completed without re-raising


class TestRateLimiter:
    def test_burst_then_block(self) -> None:
        clock = [0.0]
        limiter = RateLimiter(capacity=3, refill_per_sec=1.0, clock=lambda: clock[0])
        assert limiter.acquire("k") is True
        assert limiter.acquire("k") is True
        assert limiter.acquire("k") is True
        assert limiter.acquire("k") is False  # bucket empty

    def test_refill_over_time(self) -> None:
        clock = [0.0]
        limiter = RateLimiter(capacity=2, refill_per_sec=1.0, clock=lambda: clock[0])
        limiter.acquire("k")
        limiter.acquire("k")
        assert limiter.acquire("k") is False
        clock[0] = 1.5  # 1.5 tokens worth of refill — at least 1 available
        assert limiter.acquire("k") is True

    def test_keys_are_independent(self) -> None:
        limiter = RateLimiter(capacity=1, refill_per_sec=0.0)
        assert limiter.acquire("a") is True
        assert limiter.acquire("a") is False
        assert limiter.acquire("b") is True

    def test_stale_buckets_evicted(self) -> None:
        now = [0.0]
        limiter = RateLimiter(capacity=5, refill_per_sec=0.0, clock=lambda: now[0])
        limiter._EVICT_EVERY = 3  # type: ignore[assignment]
        limiter._BUCKET_TTL = 10.0  # type: ignore[assignment]
        limiter.acquire("old-key")  # call 1 — last-seen = 0.0
        now[0] = 20.0  # advance past TTL
        limiter.acquire("new-key")  # call 2
        limiter.acquire("new-key")  # call 3 — triggers evict
        assert "old-key" not in limiter._buckets
        assert "new-key" in limiter._buckets

    def test_exact_cost_boundary(self) -> None:
        """tokens == cost must succeed; tokens < cost must fail. Guards the
        `if tokens < cost` predicate against a `<=` mutation."""
        limiter = RateLimiter(capacity=1, refill_per_sec=0.0)
        # First acquire: tokens=1.0, cost=1.0 → 1.0 >= 1.0 → succeed.
        assert limiter.acquire("k", cost=1.0) is True
        # Second acquire: tokens=0.0, cost=1.0 → 0.0 < 1.0 → fail.
        assert limiter.acquire("k", cost=1.0) is False

    def test_zero_cost_always_succeeds(self) -> None:
        # cost=0 is degenerate but shouldn't panic; always succeeds.
        limiter = RateLimiter(capacity=1, refill_per_sec=0.0)
        for _ in range(10):
            assert limiter.acquire("k", cost=0.0) is True

    def test_refill_capped_at_capacity(self) -> None:
        clock = [0.0]
        limiter = RateLimiter(capacity=2, refill_per_sec=10.0, clock=lambda: clock[0])
        limiter.acquire("k")  # tokens: 2 -> 1
        limiter.acquire("k")  # tokens: 1 -> 0
        clock[0] = 100.0  # 100s × 10/s = 1000 tokens worth of refill
        # Refill must be capped at capacity (2), not 1000.
        assert limiter.acquire("k") is True  # tokens: 2 -> 1
        assert limiter.acquire("k") is True  # tokens: 1 -> 0
        assert limiter.acquire("k") is False  # tokens: 0


class TestRateLimitReplyError:
    """Cover the error path where Discord rejects the rate-limit reply itself."""

    async def test_rate_limit_reply_http_error_swallowed(self) -> None:
        """If `interaction.response.send_message` raises HTTPException
        when emitting the rate-limit notice, the handler must not crash."""
        from discord import HTTPException

        from discord_agent_secretary.handlers import RateLimiter

        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="X-1", title="t")
        )
        limiter = RateLimiter(capacity=1, refill_per_sec=0.0)
        register_handlers(tree, backend, guild_id=42, rate_limiter=limiter)

        cmd = tree.get_command("task", guild=__import__("discord").Object(id=42))
        assert cmd is not None
        callback = cmd.callback

        # First call drains the bucket.
        i1 = _make_interaction(user_id=99, guild_id=42)
        i1.response.defer = AsyncMock()
        await callback(i1, title="ok")

        # Second call hits rate limit; response.send_message raises.
        i2 = _make_interaction(user_id=99, guild_id=42)
        i2.response.send_message = AsyncMock(
            side_effect=HTTPException(
                response=MagicMock(status=503, reason="x"),
                message="boom",
            )
        )
        # Must not propagate.
        await callback(i2, title="blocked")

        # The exception was swallowed; backend was not called.
        assert backend.create_issue.await_count == 1


class TestEarlyReturnAfterError:
    async def test_status_handler_returns_early_when_ref_is_none(self) -> None:
        """If the backend errors out, _safe_invoke returns None and the
        handler must not produce a success-shaped followup."""
        client, tree = build_client()
        backend = MagicMock()
        backend.update_status = AsyncMock(side_effect=BackendTimeoutError())
        register_handlers(tree, backend, guild_id=42)

        from discord import Object
        cmd = tree.get_command("status", guild=Object(id=42))
        assert cmd is not None
        choice = MagicMock()
        choice.value = "in_progress"

        interaction = _make_interaction(guild_id=42)
        interaction.response.defer = AsyncMock()
        await cmd.callback(interaction, issue_id="X-1", status=choice)

        # Followup is the timeout reply only — no second success message.
        assert interaction.followup.send.await_count == 1
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs.get("ephemeral") is True

    async def test_assign_handler_returns_early_when_ref_is_none(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.assign_issue = AsyncMock(side_effect=BackendTimeoutError())
        register_handlers(tree, backend, guild_id=42)

        from discord import Object
        cmd = tree.get_command("assign", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(guild_id=42)
        interaction.response.defer = AsyncMock()
        await cmd.callback(interaction, issue_id="X-1", to="bob")

        assert interaction.followup.send.await_count == 1
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs.get("ephemeral") is True


class TestUnexpectedBackendError:
    async def test_unknown_backend_error_maps_to_user_fail(self) -> None:
        from discord_agent_secretary.backends import BackendError
        from discord_agent_secretary.handlers import _safe_invoke

        interaction = _make_interaction()

        async def boom() -> None:
            raise BackendError("something nobody planned for")

        result = await _safe_invoke(interaction, boom(), label="test")
        assert result is None
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs.get("ephemeral") is True


class TestCircuitOpenPath:
    async def test_circuit_open_uses_dedicated_message(self) -> None:
        from discord_agent_secretary.backends import CircuitOpenError
        from discord_agent_secretary.handlers import _safe_invoke

        interaction = _make_interaction()

        async def boom() -> None:
            raise CircuitOpenError("breaker tripped")

        result = await _safe_invoke(interaction, boom(), label="test")
        assert result is None
        msg = interaction.followup.send.call_args.args[0]
        assert "недоступен" in msg or "Tracker" in msg
        assert interaction.followup.send.call_args.kwargs.get("ephemeral") is True


class TestStatusAndAssignHappyPaths:
    """Round-trip the closures registered by `register_handlers`."""

    async def test_status_cmd_invokes_update_status(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.update_status = AsyncMock(return_value=IssueRef(id="X-1"))
        register_handlers(tree, backend, guild_id=42)

        from discord import Object
        cmd = tree.get_command("status", guild=Object(id=42))
        assert cmd is not None
        choice = MagicMock()
        choice.value = "in_progress"

        interaction = _make_interaction(guild_id=42)
        interaction.response.defer = AsyncMock()
        await cmd.callback(interaction, issue_id="X-1", status=choice)

        backend.update_status.assert_awaited_once_with("X-1", "in_progress")
        interaction.followup.send.assert_awaited_once()

    async def test_assign_cmd_invokes_assign_issue(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.assign_issue = AsyncMock(return_value=IssueRef(id="X-2"))
        register_handlers(tree, backend, guild_id=42)

        from discord import Object
        cmd = tree.get_command("assign", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(guild_id=42)
        interaction.response.defer = AsyncMock()
        await cmd.callback(interaction, issue_id="X-2", to="alice")

        backend.assign_issue.assert_awaited_once_with("X-2", "alice")
        interaction.followup.send.assert_awaited_once()

    async def test_task_cmd_short_circuits_on_backend_failure(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(side_effect=BackendTimeoutError())
        register_handlers(tree, backend, guild_id=42)

        from discord import Object
        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(guild_id=42)
        interaction.response.defer = AsyncMock()
        await cmd.callback(interaction, title="x")

        # Followup is the timeout reply only — no success message.
        assert interaction.followup.send.await_count == 1
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs.get("ephemeral") is True


class TestRateLimitInHandler:
    async def test_rate_limit_blocks_backend_call(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="X-1", title="t")
        )
        # Capacity 1, no refill: first call passes, second is rate-limited.
        limiter = RateLimiter(capacity=1, refill_per_sec=0.0)
        register_handlers(tree, backend, guild_id=42, rate_limiter=limiter)

        cmd = tree.get_command("task", guild=__import__("discord").Object(id=42))
        assert cmd is not None
        callback = cmd.callback

        i1 = _make_interaction(user_id=1, guild_id=42)
        i1.response.defer = AsyncMock()
        await callback(i1, title="first")

        i2 = _make_interaction(user_id=1, guild_id=42)
        i2.response.defer = AsyncMock()
        await callback(i2, title="second")

        # Backend was called once (first interaction); second got the
        # ephemeral rate-limit reply and never reached the backend.
        assert backend.create_issue.await_count == 1
        i2.response.send_message.assert_awaited_once()
        kwargs = i2.response.send_message.call_args.kwargs
        assert kwargs.get("ephemeral") is True
