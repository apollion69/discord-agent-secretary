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


class TestTaskCreatedMessage:
    """Verify the success message format for /task."""

    async def test_link_when_identifier_and_app_url(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="uuid-1", title="Fix bug", identifier="VEN-99")
        )
        register_handlers(tree, backend, guild_id=42, app_url="http://multica.local:3000")

        from discord import Object
        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(guild_id=42)
        interaction.response.defer = AsyncMock()
        await cmd.callback(interaction, title="Fix bug")

        msg = interaction.followup.send.call_args.args[0]
        assert "[VEN-99]" in msg
        assert "http://multica.local:3000/venchur/issues/VEN-99" in msg
        assert "uuid-1" not in msg

    async def test_fallback_to_identifier_without_app_url(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="uuid-2", title="t", identifier="VEN-7")
        )
        register_handlers(tree, backend, guild_id=42, app_url="")

        from discord import Object
        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(guild_id=42)
        interaction.response.defer = AsyncMock()
        await cmd.callback(interaction, title="t")

        msg = interaction.followup.send.call_args.args[0]
        assert "VEN-7" in msg
        assert "uuid-2" not in msg

    async def test_fallback_to_uuid_when_no_identifier(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="uuid-3", title="t")
        )
        register_handlers(tree, backend, guild_id=42, app_url="http://multica.local:3000")

        from discord import Object
        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(guild_id=42)
        interaction.response.defer = AsyncMock()
        await cmd.callback(interaction, title="t")

        msg = interaction.followup.send.call_args.args[0]
        assert "uuid-3" in msg

    async def test_app_url_trailing_slash_stripped(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="uuid-4", title="t", identifier="VEN-10")
        )
        register_handlers(tree, backend, guild_id=42, app_url="http://multica.local:3000/")

        from discord import Object
        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(guild_id=42)
        interaction.response.defer = AsyncMock()
        await cmd.callback(interaction, title="t")

        msg = interaction.followup.send.call_args.args[0]
        assert "http://multica.local:3000/venchur/issues/VEN-10" in msg
        assert "//" not in msg.split("http://", 1)[1]


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


class TestActAsMember:
    """`/task` attributes the issue to the real Discord requester via on_behalf_of."""

    async def test_mapped_invoker_passes_on_behalf_of(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(return_value=IssueRef(id="uuid-1", title="t"))
        register_handlers(
            tree, backend, guild_id=42, member_map={"7": "member-uuid-7"}
        )

        from discord import Object

        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(user_id=7, guild_id=42)
        interaction.response.defer = AsyncMock()
        await cmd.callback(interaction, title="t")

        assert backend.create_issue.call_args.kwargs["on_behalf_of"] == "member-uuid-7"

    async def test_unmapped_invoker_falls_back_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(return_value=IssueRef(id="uuid-2", title="t"))
        register_handlers(
            tree, backend, guild_id=42, member_map={"999": "member-uuid-999"}
        )

        from discord import Object

        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(user_id=7, guild_id=42)
        interaction.response.defer = AsyncMock()
        with caplog.at_level("WARNING"):
            await cmd.callback(interaction, title="t")

        # Task still created, attributed to the token owner (on_behalf_of=None).
        assert backend.create_issue.call_args.kwargs["on_behalf_of"] is None
        interaction.followup.send.assert_awaited()
        assert any("no Multica member mapping" in r.message for r in caplog.records)

    async def test_no_map_passes_none_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(return_value=IssueRef(id="uuid-3", title="t"))
        register_handlers(tree, backend, guild_id=42)

        from discord import Object

        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(user_id=7, guild_id=42)
        interaction.response.defer = AsyncMock()
        with caplog.at_level("WARNING"):
            await cmd.callback(interaction, title="t")

        assert backend.create_issue.call_args.kwargs["on_behalf_of"] is None
        assert not any("no Multica member mapping" in r.message for r in caplog.records)

    async def test_no_invoker_id_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A map is configured but the interaction carried no user — we must NOT
        # emit the "no mapping for Discord user" warning (the wording misleads).
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(return_value=IssueRef(id="uuid-4", title="t"))
        register_handlers(tree, backend, guild_id=42, member_map={"7": "member-uuid-7"})

        from discord import Object

        cmd = tree.get_command("task", guild=Object(id=42))
        assert cmd is not None

        interaction = _make_interaction(user_id=7, guild_id=42)
        interaction.user = None  # no invoker on the interaction
        interaction.response.defer = AsyncMock()
        with caplog.at_level("WARNING"):
            await cmd.callback(interaction, title="t")

        assert backend.create_issue.call_args.kwargs["on_behalf_of"] is None
        assert not any("no Multica member mapping" in r.message for r in caplog.records)


class TestTaskThread:
    """`/task` thread-per-task: opens a Discord thread and pings inside it."""

    @staticmethod
    def _msg_with_thread() -> MagicMock:
        thread = MagicMock()
        thread.send = AsyncMock()
        msg = MagicMock()
        msg.create_thread = AsyncMock(return_value=thread)
        return msg

    @staticmethod
    def _task_cmd(tree: object) -> object:
        from discord import Object

        cmd = tree.get_command("task", guild=Object(id=42))  # type: ignore[attr-defined]
        assert cmd is not None
        return cmd

    async def test_disabled_by_default_opens_no_thread(self) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="u1", title="t", identifier="VEN-1")
        )
        register_handlers(tree, backend, guild_id=42)  # no thread_config → off

        cmd = self._task_cmd(tree)
        interaction = _make_interaction(user_id=7, guild_id=42)
        interaction.response.defer = AsyncMock()
        msg = self._msg_with_thread()
        interaction.followup.send = AsyncMock(return_value=msg)
        await cmd.callback(interaction, title="t")  # type: ignore[attr-defined]

        msg.create_thread.assert_not_called()

    async def test_enabled_opens_public_thread_and_pings_creator_and_watchers(self) -> None:
        from discord_agent_secretary.threads import ThreadConfig

        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="u1", title="Fix login redirect", identifier="VEN-9")
        )
        register_handlers(
            tree,
            backend,
            guild_id=42,
            app_url="http://m.local:3000",
            thread_config=ThreadConfig(enabled=True, name_max_words=6, ping_user_ids=(555,)),
        )

        cmd = self._task_cmd(tree)
        interaction = _make_interaction(user_id=7, guild_id=42)
        interaction.response.defer = AsyncMock()
        msg = self._msg_with_thread()
        interaction.followup.send = AsyncMock(return_value=msg)
        await cmd.callback(interaction, title="Fix login redirect")  # type: ignore[attr-defined]

        msg.create_thread.assert_awaited_once()
        name = msg.create_thread.call_args.kwargs["name"]
        assert name.startswith("VEN-9")
        thread = msg.create_thread.return_value
        thread.send.assert_awaited_once()
        intro = thread.send.call_args.args[0]
        assert "<@7>" in intro  # creator
        assert "<@555>" in intro  # configured watcher
        am = thread.send.call_args.kwargs["allowed_mentions"]
        assert am.everyone is False
        assert {o.id for o in am.users} == {7, 555}

    async def test_enabled_pings_assignee_resolved_via_member_map(self) -> None:
        from discord_agent_secretary.threads import ThreadConfig

        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="u1", title="t", identifier="VEN-2")
        )
        register_handlers(
            tree,
            backend,
            guild_id=42,
            member_map={"999": "member-uuid-7"},  # reverse: member-uuid-7 -> 999
            thread_config=ThreadConfig(enabled=True),
        )

        cmd = self._task_cmd(tree)
        interaction = _make_interaction(user_id=7, guild_id=42)
        interaction.response.defer = AsyncMock()
        msg = self._msg_with_thread()
        interaction.followup.send = AsyncMock(return_value=msg)
        await cmd.callback(interaction, title="t", assignee="member-uuid-7")  # type: ignore[attr-defined]

        intro = msg.create_thread.return_value.send.call_args.args[0]
        assert "<@999>" in intro  # assignee resolved to Discord

    async def test_enabled_private_uses_channel_create_thread(self) -> None:
        import discord

        from discord_agent_secretary.threads import ThreadConfig

        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="u1", title="t", identifier="VEN-3")
        )
        register_handlers(
            tree,
            backend,
            guild_id=42,
            thread_config=ThreadConfig(enabled=True, private=True, auto_archive_minutes=1440),
        )

        cmd = self._task_cmd(tree)
        interaction = _make_interaction(user_id=7, guild_id=42)
        interaction.response.defer = AsyncMock()
        msg = MagicMock()
        msg.create_thread = AsyncMock()  # the public path must NOT be used
        interaction.followup.send = AsyncMock(return_value=msg)
        thread = MagicMock()
        thread.send = AsyncMock()
        interaction.channel.create_thread = AsyncMock(return_value=thread)
        await cmd.callback(interaction, title="t")  # type: ignore[attr-defined]

        interaction.channel.create_thread.assert_awaited_once()
        kwargs = interaction.channel.create_thread.call_args.kwargs
        assert kwargs["type"] == discord.ChannelType.private_thread
        assert kwargs["auto_archive_duration"] == 1440
        msg.create_thread.assert_not_called()

    async def test_thread_failure_does_not_break_task(self) -> None:
        from discord import HTTPException

        from discord_agent_secretary.threads import ThreadConfig

        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="u1", title="t", identifier="VEN-4")
        )
        register_handlers(
            tree, backend, guild_id=42, thread_config=ThreadConfig(enabled=True)
        )

        cmd = self._task_cmd(tree)
        interaction = _make_interaction(user_id=7, guild_id=42)
        interaction.response.defer = AsyncMock()
        msg = MagicMock()
        msg.create_thread = AsyncMock(
            side_effect=HTTPException(
                response=MagicMock(status=403, reason="x"), message="no perms"
            )
        )
        interaction.followup.send = AsyncMock(return_value=msg)
        # The thread API blew up, but the command must still complete cleanly —
        # the confirmation followup already went out.
        await cmd.callback(interaction, title="t")  # type: ignore[attr-defined]
        interaction.followup.send.assert_awaited()

    async def test_enabled_keeps_main_channel_confirmation_contract(self) -> None:
        # AC8: enabling threads must not change the existing main-channel reply.
        from discord_agent_secretary.threads import ThreadConfig

        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="u1", title="Fix bug", identifier="VEN-9")
        )
        register_handlers(
            tree,
            backend,
            guild_id=42,
            app_url="http://m.local:3000",
            thread_config=ThreadConfig(enabled=True),
        )

        cmd = self._task_cmd(tree)
        interaction = _make_interaction(user_id=7, guild_id=42)
        interaction.response.defer = AsyncMock()
        msg = self._msg_with_thread()
        interaction.followup.send = AsyncMock(return_value=msg)
        await cmd.callback(interaction, title="Fix bug")  # type: ignore[attr-defined]

        confirmation = interaction.followup.send.call_args.args[0]
        assert "✅ Создана задача" in confirmation
        assert "[VEN-9]" in confirmation
        assert "http://m.local:3000/venchur/issues/VEN-9" in confirmation
