"""Unit tests for discord_agent_secretary.main.

The bot's network path can't run in CI, so these tests cover:
  * `_collect_secrets` — secret-field extraction
  * `resolve_bot_member` — cache hit / miss + REST fallback
  * `verify_guilds_safe` — happy path + abort paths
  * `sync_commands` — guild-scoped vs global
  * `install_signal_handlers` — Linux happy path + Windows fallback log
  * `_shutdown_healthcheck` — None passthrough, OSError swallow
  * `main()` entry-point smoke (token-missing / LoginFailure / KbdInt / clean)
"""
from __future__ import annotations

import asyncio
import logging
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from discord_agent_secretary.discord_client import UnsafePermissionsError
from discord_agent_secretary.main import (
    _collect_secrets,
    _shutdown_healthcheck,
    install_signal_handlers,
    main,
    resolve_bot_member,
    sync_commands,
    verify_guilds_safe,
)

pytestmark = pytest.mark.unit


class TestCollectSecrets:
    def test_pulls_each_known_field(self) -> None:
        settings = MagicMock()
        settings.discord_bot_token = "discord-secret-value"
        settings.github_token = "ghp_token"
        settings.linear_api_key = "lin_api"
        settings.jira_api_token = "jira_tok"
        settings.anthropic_api_key = "anthropic_tok"
        result = _collect_secrets(settings)
        assert "discord-secret-value" in result
        assert "ghp_token" in result
        assert "lin_api" in result
        assert "jira_tok" in result
        assert "anthropic_tok" in result

    def test_skips_non_string_values(self) -> None:
        settings = MagicMock()
        settings.discord_bot_token = "real-token"
        settings.github_token = None
        settings.linear_api_key = 12345
        settings.jira_api_token = ""
        settings.anthropic_api_key = "anth"
        result = _collect_secrets(settings)
        assert "real-token" in result
        assert "anth" in result
        assert None not in result
        assert 12345 not in result


class TestResolveBotMember:
    async def test_none_user_id_returns_none(self) -> None:
        guild = MagicMock()
        result = await resolve_bot_member(guild, None)
        assert result is None
        guild.get_member.assert_not_called()

    async def test_cache_hit_skips_rest(self) -> None:
        guild = MagicMock()
        guild.get_member.return_value = MagicMock(name="cached_member")
        result = await resolve_bot_member(guild, 42)
        assert result is guild.get_member.return_value

    async def test_cache_miss_falls_through_to_fetch(self) -> None:
        guild = MagicMock()
        guild.id = 7
        guild.get_member.return_value = None
        guild.fetch_member = AsyncMock(return_value=MagicMock(name="fetched"))
        result = await resolve_bot_member(guild, 42)
        assert result is guild.fetch_member.return_value
        guild.fetch_member.assert_awaited_once_with(42)

    async def test_fetch_http_error_returns_none(self, caplog) -> None:
        guild = MagicMock()
        guild.id = 7
        guild.get_member.return_value = None
        guild.fetch_member = AsyncMock(
            side_effect=discord.HTTPException(
                response=MagicMock(status=502, reason="bad gw"),
                message="upstream",
            )
        )
        with caplog.at_level(logging.WARNING):
            result = await resolve_bot_member(guild, 42)
        assert result is None
        assert any("fetch_member failed" in r.message for r in caplog.records)


class TestVerifyGuildsSafe:
    async def test_no_guilds_returns_true(self) -> None:
        client = MagicMock()
        client.user = MagicMock(id=42)
        client.guilds = []
        assert await verify_guilds_safe(client) is True

    async def test_safe_perms_returns_true(self) -> None:
        client = MagicMock()
        client.user = MagicMock(id=42)
        guild = MagicMock(id=1, name="ok")
        member = MagicMock()
        member.guild_permissions = MagicMock()
        # Set ALL perm flags False on the mock — REFUSE_PERMS lookup uses getattr.
        for attr in (
            "administrator", "manage_guild", "manage_roles", "manage_channels",
            "manage_webhooks", "ban_members", "kick_members", "mention_everyone",
        ):
            setattr(member.guild_permissions, attr, False)
        guild.get_member.return_value = member
        client.guilds = [guild]
        assert await verify_guilds_safe(client) is True

    async def test_unresolvable_member_returns_false(self, caplog) -> None:
        client = MagicMock()
        client.user = MagicMock(id=42)
        guild = MagicMock(id=1, name="hidden")
        guild.get_member.return_value = None
        guild.fetch_member = AsyncMock(
            side_effect=discord.HTTPException(
                response=MagicMock(status=403, reason="forbidden"),
                message="no",
            )
        )
        client.guilds = [guild]
        with caplog.at_level(logging.CRITICAL):
            assert await verify_guilds_safe(client) is False
        assert any(
            "membership unresolved" in r.message for r in caplog.records
        )

    async def test_unsafe_perms_returns_false(self, caplog) -> None:
        client = MagicMock()
        client.user = MagicMock(id=42)
        guild = MagicMock(id=1, name="bad")
        member = MagicMock()
        # Build a bot-specific role (id != guild.id) with administrator=True.
        # The @everyone role (id == guild.id) is excluded by assert_safe_permissions
        # so the dangerous perm must come from a bot-specific role to trigger the check.
        bot_role = MagicMock()
        bot_role.id = 999  # not guild.id (1)
        bot_role.permissions = discord.Permissions(administrator=True)
        everyone_role = MagicMock()
        everyone_role.id = guild.id  # @everyone — excluded from check
        everyone_role.permissions = discord.Permissions.none()
        member.roles = [everyone_role, bot_role]
        guild.get_member.return_value = member
        client.guilds = [guild]

        with caplog.at_level(logging.CRITICAL):
            assert await verify_guilds_safe(client) is False
        assert any("refusing to run" in r.message for r in caplog.records)

    async def test_unsafe_perms_propagates_through_assert(self) -> None:
        # If `assert_safe_permissions` raises something other than
        # UnsafePermissionsError, that's a programming error and should
        # propagate — we only catch the documented sentinel.
        client = MagicMock()
        client.user = MagicMock(id=42)
        guild = MagicMock(id=1, name="oops")
        member = MagicMock()
        guild.get_member.return_value = member
        client.guilds = [guild]
        with patch(
            "discord_agent_secretary.main.assert_safe_permissions",
            side_effect=RuntimeError("bug in perm check"),
        ):
            with pytest.raises(RuntimeError):
                await verify_guilds_safe(client)

    async def test_unsafe_perms_via_real_assert(self) -> None:
        client = MagicMock()
        client.user = MagicMock(id=42)
        guild = MagicMock(id=1, name="bad")
        member = MagicMock()
        client.guilds = [guild]
        guild.get_member.return_value = member
        with patch(
            "discord_agent_secretary.main.assert_safe_permissions",
            side_effect=UnsafePermissionsError("admin granted"),
        ):
            assert await verify_guilds_safe(client) is False


class TestSyncCommands:
    async def test_guild_scoped(self) -> None:
        tree = MagicMock()
        tree.sync = AsyncMock(return_value=[1, 2, 3])
        count = await sync_commands(tree, guild_id=42)
        assert count == 3
        # We called the guild-scoped form.
        assert tree.sync.await_args.kwargs.get("guild") is not None

    async def test_global(self) -> None:
        tree = MagicMock()
        tree.sync = AsyncMock(return_value=[1, 2])
        count = await sync_commands(tree, guild_id=None)
        assert count == 2
        # No guild kwarg in the global path.
        assert tree.sync.await_args.kwargs == {} or tree.sync.await_args.kwargs.get("guild") is None


class TestInstallSignalHandlers:
    def test_registers_sigterm_and_sigint(self) -> None:
        loop = MagicMock()
        loop.add_signal_handler = MagicMock()
        cb = MagicMock()
        registered = install_signal_handlers(loop, cb)
        assert set(registered) == {signal.SIGTERM, signal.SIGINT}
        assert loop.add_signal_handler.call_count == 2

    def test_swallows_notimplemented(self, caplog) -> None:
        # Windows path — `loop.add_signal_handler` raises NotImplementedError.
        loop = MagicMock()
        loop.add_signal_handler = MagicMock(side_effect=NotImplementedError)
        with caplog.at_level(logging.WARNING):
            registered = install_signal_handlers(loop, lambda: None)
        assert registered == []
        assert any("signal handler not registered" in r.message for r in caplog.records)

    def test_swallows_runtime_error(self) -> None:
        # Non-main-thread path — RuntimeError.
        loop = MagicMock()
        loop.add_signal_handler = MagicMock(side_effect=RuntimeError("not main"))
        registered = install_signal_handlers(loop, lambda: None)
        assert registered == []


class TestShutdownHealthcheck:
    def test_none_handle_is_safe(self) -> None:
        _shutdown_healthcheck(None)  # must not raise

    def test_clean_shutdown(self) -> None:
        handle = MagicMock()
        _shutdown_healthcheck(handle)
        handle.shutdown.assert_called_once()

    def test_oserror_is_swallowed(self, caplog) -> None:
        handle = MagicMock()
        handle.shutdown.side_effect = OSError("socket already closed")
        with caplog.at_level(logging.WARNING):
            _shutdown_healthcheck(handle)
        assert any(
            "health server shutdown raised" in r.message for r in caplog.records
        )

    def test_non_oserror_propagates(self) -> None:
        # We only swallow the documented socket-teardown noise; real bugs
        # (e.g. a programming error in the handle) must surface.
        handle = MagicMock()
        handle.shutdown.side_effect = RuntimeError("bug")
        with pytest.raises(RuntimeError):
            _shutdown_healthcheck(handle)


def _make_runner_stub(*, raise_exc: BaseException | None = None):
    """Build an `asyncio.run` replacement that closes the coroutine argument.

    Without closing it, Python issues a `coroutine was never awaited`
    warning that pollutes the test output (and looks like a real bug).
    """

    def _stub(coro, *args, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        if raise_exc is not None:
            raise raise_exc
        return None

    return _stub


class TestMainEntrypoint:
    def test_missing_token_returns_one(self, monkeypatch, clean_settings) -> None:
        # No DISCORD_BOT_TOKEN -> early exit with code 1, no Discord call.
        with patch("discord_agent_secretary.main.asyncio.run") as runner:
            rc = main()
        assert rc == 1
        runner.assert_not_called()

    def test_login_failure_returns_one(self, monkeypatch, clean_settings) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
        with (
            patch("discord_agent_secretary.main.make_backend") as make_backend,
            patch("discord_agent_secretary.main.asyncio.run") as runner,
        ):
            make_backend.return_value = MagicMock()
            runner.side_effect = _make_runner_stub(
                raise_exc=discord.LoginFailure("bad token")
            )
            rc = main()
        assert rc == 1

    def test_keyboard_interrupt_returns_zero(self, monkeypatch, clean_settings) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
        with (
            patch("discord_agent_secretary.main.make_backend") as make_backend,
            patch("discord_agent_secretary.main.asyncio.run") as runner,
        ):
            make_backend.return_value = MagicMock()
            runner.side_effect = _make_runner_stub(raise_exc=KeyboardInterrupt())
            rc = main()
        assert rc == 0

    def test_clean_run_returns_zero(self, monkeypatch, clean_settings) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
        with (
            patch("discord_agent_secretary.main.make_backend") as make_backend,
            patch("discord_agent_secretary.main.asyncio.run") as runner,
        ):
            make_backend.return_value = MagicMock()
            runner.side_effect = _make_runner_stub()
            rc = main()
        assert rc == 0

    def test_settings_failure_returns_one(self, monkeypatch, clean_settings, capsys) -> None:
        # If get_settings raises (e.g. ValidationError), main exits 1
        # and prints to stderr.
        with patch(
            "discord_agent_secretary.main.get_settings",
            side_effect=RuntimeError("config blew up"),
        ):
            rc = main()
        captured = capsys.readouterr()
        assert rc == 1
        assert "configuration failed" in captured.err

    def test_aborted_state_returns_one(self, monkeypatch, clean_settings) -> None:
        # If on_ready aborted the bot (unsafe perms), main returns 1 even
        # though asyncio.run completed without raising.
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")

        def _capture_state_runner(coro, *args, **kwargs):
            # The body of main() created the _RunState before this call.
            # Find it on the frame stack — easier to verify integration
            # through an equivalent check: just patch asyncio.run to no-op
            # and patch _RunState to emit aborted=True via monkeypatch on
            # the module.
            if hasattr(coro, "close"):
                coro.close()
            return None

        from dataclasses import dataclass

        @dataclass
        class _AbortedState:
            aborted: bool = True
            synced: bool = False

        with (
            patch("discord_agent_secretary.main.make_backend") as make_backend,
            patch("discord_agent_secretary.main.asyncio.run", side_effect=_capture_state_runner),
            patch("discord_agent_secretary.main._RunState", _AbortedState),
        ):
            make_backend.return_value = MagicMock()
            rc = main()
        assert rc == 1


class TestRunClient:
    """Cover `run_client` graceful-close path without touching real Discord."""

    async def test_install_signal_then_start(self) -> None:
        # `run_client` must register signal handlers and then `await client.start`.
        from discord_agent_secretary.main import run_client

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.start = AsyncMock(return_value=None)
        client.close = AsyncMock(return_value=None)

        with patch(
            "discord_agent_secretary.main.install_signal_handlers"
        ) as install:
            await run_client(client, "fake-token")
        install.assert_called_once()
        client.start.assert_awaited_once_with("fake-token")

    async def test_signal_callback_holds_close_task(self) -> None:
        # The shutdown task created by the signal callback must be retained
        # in a strong-reference set; otherwise PEP 3156 GC can reap it
        # mid-flight on 3.11+.
        from discord_agent_secretary.main import run_client

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.close = AsyncMock(return_value=None)

        captured_callback: list = []

        def _capture(loop, cb):  # type: ignore[no-untyped-def]
            captured_callback.append(cb)
            return [signal.SIGTERM, signal.SIGINT]

        async def _start_then_close(_token):
            # Fire the signal callback while client.start "is running" — it
            # should schedule client.close() and not raise.
            assert captured_callback, "signal handler should be installed before start"
            captured_callback[0]()
            # Yield once so the close task can run.
            await asyncio.sleep(0)

        client.start = AsyncMock(side_effect=_start_then_close)

        with patch(
            "discord_agent_secretary.main.install_signal_handlers",
            side_effect=_capture,
        ):
            await run_client(client, "fake-token")

        client.close.assert_awaited()
