"""Unit tests for discord_agent_secretary.main.

The bot's network path can't run in CI, so these tests cover the
synchronous portions: secret collection, missing-token early exit, and
the LoginFailure exit path.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import discord
import pytest

from discord_agent_secretary.main import _collect_secrets, main

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
