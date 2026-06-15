"""Unit tests for discord_agent_secretary.observer (passive secretary)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from discord_agent_secretary.backends import BackendTimeoutError, IssueRef
from discord_agent_secretary.observer import (
    DEFAULT_TRIGGERS,
    ObserverContext,
    TaskConfirmView,
    build_preview,
    detect_trigger,
    handle_observed_message,
    register_message_observer,
)
from discord_agent_secretary.parsers import ParsedTask
from discord_agent_secretary.threads import ThreadConfig

pytestmark = pytest.mark.unit


class TestDetectTrigger:
    def test_matches_default_prefixes(self) -> None:
        assert detect_trigger("/task fix login", DEFAULT_TRIGGERS) == "fix login"
        assert detect_trigger("!task fix login", DEFAULT_TRIGGERS) == "fix login"
        assert detect_trigger("задача: почини логин", DEFAULT_TRIGGERS) == "почини логин"

    def test_case_insensitive(self) -> None:
        assert detect_trigger("/TASK do it", DEFAULT_TRIGGERS) == "do it"

    def test_leading_whitespace_tolerated(self) -> None:
        assert detect_trigger("   /task x", DEFAULT_TRIGGERS) == "x"

    def test_empty_body_returns_none(self) -> None:
        assert detect_trigger("/task   ", DEFAULT_TRIGGERS) is None

    def test_no_trigger_returns_none(self) -> None:
        assert detect_trigger("just chatting", DEFAULT_TRIGGERS) is None


class TestBuildPreview:
    def test_includes_fields(self) -> None:
        p = ParsedTask(title="Fix login", priority="high", deadline="2026-07-01", assignee="bob")
        text = build_preview(p)
        assert "Fix login" in text
        assert "high" in text
        assert "2026-07-01" in text
        assert "@bob" in text

    def test_assignee_id_rendered(self) -> None:
        p = ParsedTask(title="t", assignee_id=42)
        assert "<@42>" in build_preview(p)

    def test_minimal(self) -> None:
        assert "Завести задачу" in build_preview(ParsedTask(title="t"))


def _message(content: str, *, author_id: int = 7, bot: bool = False, channel_id: int = 100) -> MagicMock:
    m = MagicMock()
    m.author.id = author_id
    m.author.bot = bot
    m.channel.id = channel_id
    m.content = content
    m.reply = AsyncMock()
    return m


def _kw() -> dict:
    return dict(
        client_user_id=999,
        backend=MagicMock(),
        watch_channels={100},
        triggers=DEFAULT_TRIGGERS,
        app_url="http://m.local",
        member_map={},
        reverse_member_map={},
        thread_config=ThreadConfig(),
    )


class TestHandleObservedMessage:
    async def test_valid_message_posts_confirmation(self) -> None:
        msg = _message("/task fix the login bug")
        view = await handle_observed_message(msg, **_kw())
        assert isinstance(view, TaskConfirmView)
        msg.reply.assert_awaited_once()
        # preview must not mass-ping
        kwargs = msg.reply.call_args.kwargs
        assert kwargs["allowed_mentions"].everyone is False

    async def test_bot_author_ignored(self) -> None:
        msg = _message("/task x", bot=True)
        assert await handle_observed_message(msg, **_kw()) is None
        msg.reply.assert_not_called()

    async def test_self_message_ignored(self) -> None:
        msg = _message("/task x", author_id=999)  # == client_user_id
        assert await handle_observed_message(msg, **_kw()) is None

    async def test_off_channel_ignored(self) -> None:
        msg = _message("/task x", channel_id=555)
        assert await handle_observed_message(msg, **_kw()) is None

    async def test_no_trigger_ignored(self) -> None:
        msg = _message("just a normal message")
        assert await handle_observed_message(msg, **_kw()) is None

    async def test_empty_body_ignored(self) -> None:
        msg = _message("/task    ")
        assert await handle_observed_message(msg, **_kw()) is None


def _thread() -> MagicMock:
    t = MagicMock()
    t.send = AsyncMock()
    return t


def _confirm_interaction(*, user_id: int = 7) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.edit_message = AsyncMock()
    interaction.message = MagicMock()
    interaction.message.create_thread = AsyncMock(return_value=_thread())
    interaction.channel = MagicMock()
    return interaction


class TestTaskConfirmView:
    def _ctx(self, *, thread_enabled: bool = False) -> ObserverContext:
        return ObserverContext(
            backend=MagicMock(),
            parsed=ParsedTask(title="Fix login", priority="high"),
            author_id=7,
            on_behalf_of=None,
            app_url="http://m.local",
            thread_config=ThreadConfig(enabled=thread_enabled),
        )

    async def test_confirm_creates_issue_and_opens_thread(self) -> None:
        ctx = self._ctx(thread_enabled=True)
        ctx.backend.create_issue = AsyncMock(
            return_value=IssueRef(id="u1", title="Fix login", identifier="VEN-1")
        )
        view = TaskConfirmView(ctx)
        interaction = _confirm_interaction()
        await view._on_confirm(interaction)

        ctx.backend.create_issue.assert_awaited_once()
        assert ctx.backend.create_issue.call_args.kwargs["title"] == "Fix login"
        assert ctx.backend.create_issue.call_args.kwargs["priority"] == "high"
        interaction.response.edit_message.assert_awaited()  # confirmation shown
        interaction.message.create_thread.assert_awaited_once()  # thread opened

    async def test_confirm_without_assignee_uses_two_squad_routing(self) -> None:
        ctx = ObserverContext(
            backend=MagicMock(),
            parsed=ParsedTask(title="Audit services", priority="medium"),
            author_id=7,
            on_behalf_of="member-7",
            app_url="http://m.local",
            thread_config=ThreadConfig(),
            default_assignee="Claude",
            execution_assignee="GPT-5.5",
        )
        ctx.backend.create_issue = AsyncMock(
            side_effect=[
                IssueRef(id="parent-uuid", title="Audit services", identifier="VEN-5"),
                IssueRef(id="child-uuid", title="Execute: Audit services", identifier="VEN-6"),
            ]
        )
        ctx.backend.add_comment = AsyncMock()
        view = TaskConfirmView(ctx)
        interaction = _confirm_interaction()
        await view._on_confirm(interaction)

        assert ctx.backend.create_issue.await_count == 2
        parent_call, child_call = ctx.backend.create_issue.await_args_list
        assert parent_call.kwargs["assignee"] == "Claude"
        assert child_call.kwargs["assignee"] == "GPT-5.5"
        assert child_call.kwargs["parent"] == "parent-uuid"
        ctx.backend.add_comment.assert_awaited_once()
        edited = interaction.response.edit_message.await_args.kwargs["content"]
        assert "VEN-5" in edited
        assert "VEN-6" in edited

    async def test_confirm_without_thread_config_skips_thread(self) -> None:
        ctx = self._ctx(thread_enabled=False)
        ctx.backend.create_issue = AsyncMock(return_value=IssueRef(id="u1", identifier="VEN-2"))
        view = TaskConfirmView(ctx)
        interaction = _confirm_interaction()
        await view._on_confirm(interaction)

        ctx.backend.create_issue.assert_awaited_once()
        interaction.message.create_thread.assert_not_called()

    async def test_confirm_backend_error_shows_fail_no_thread(self) -> None:
        ctx = self._ctx(thread_enabled=True)
        ctx.backend.create_issue = AsyncMock(side_effect=BackendTimeoutError())
        view = TaskConfirmView(ctx)
        interaction = _confirm_interaction()
        await view._on_confirm(interaction)

        edited = interaction.response.edit_message.call_args.kwargs["content"]
        assert "Не удалось" in edited
        interaction.message.create_thread.assert_not_called()

    async def test_cancel_does_not_create(self) -> None:
        ctx = self._ctx()
        ctx.backend.create_issue = AsyncMock()
        view = TaskConfirmView(ctx)
        interaction = _confirm_interaction()
        await view._on_cancel(interaction)

        ctx.backend.create_issue.assert_not_called()
        assert "Отменено" in interaction.response.edit_message.call_args.kwargs["content"]

    async def test_interaction_check_only_author(self) -> None:
        ctx = self._ctx()
        view = TaskConfirmView(ctx)
        ok = MagicMock()
        ok.user.id = 7
        wrong = MagicMock()
        wrong.user.id = 8
        assert await view.interaction_check(ok) is True
        assert await view.interaction_check(wrong) is False


class TestRegisterObserver:
    async def test_register_wires_on_message(self) -> None:
        client = MagicMock()
        client.user.id = 999
        captured: dict[str, object] = {}

        def fake_event(coro: object) -> object:
            captured["on_message"] = coro
            return coro

        client.event = fake_event
        backend = MagicMock()
        register_message_observer(
            client,
            backend=backend,
            watch_channels=[100],
            triggers=DEFAULT_TRIGGERS,
            app_url="",
            member_map={},
            thread_config=ThreadConfig(),
        )
        on_message = captured["on_message"]
        assert callable(on_message)

        # Non-trigger message: ignored, no reply.
        m1 = _message("just chatting", channel_id=100)
        await on_message(m1)  # type: ignore[operator]
        m1.reply.assert_not_called()

        # Trigger message: posts a confirmation.
        m2 = _message("/task do a thing", channel_id=100)
        await on_message(m2)  # type: ignore[operator]
        m2.reply.assert_awaited_once()
