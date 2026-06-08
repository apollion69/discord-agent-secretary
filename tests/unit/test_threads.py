"""Unit tests for discord_agent_secretary.threads.

Pure helpers are exercised without Discord; `open_task_thread` is driven with
AsyncMock message/channel objects so the orchestration (which thread API is
called, what content is posted, and the never-raise contract) is verified in
isolation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_agent_secretary.threads import (
    THREAD_NAME_MAX,
    VALID_AUTO_ARCHIVE,
    ThreadConfig,
    ThreadPings,
    announce_task_thread,
    build_allowed_mentions,
    build_thread_intro,
    build_thread_name,
    open_task_thread,
    resolve_thread_pings,
)

pytestmark = pytest.mark.unit


class TestBuildThreadName:
    def test_identifier_plus_title(self) -> None:
        assert build_thread_name("VEN-99", "Fix login redirect") == "VEN-99 Fix login redirect"

    def test_word_cap_keeps_first_n_words(self) -> None:
        name = build_thread_name(
            "VEN-1", "fix the broken login redirect bug on staging now", max_words=5
        )
        assert name == "VEN-1 fix the broken login redirect"

    def test_zero_word_cap_keeps_all_words(self) -> None:
        name = build_thread_name("VEN-1", "a b c d e f g h", max_words=0)
        assert name == "VEN-1 a b c d e f g h"

    def test_collapses_internal_whitespace(self) -> None:
        assert build_thread_name("VEN-2", "fix   the\tlogin\n bug") == "VEN-2 fix the login bug"

    def test_hard_caps_at_100_chars_with_ellipsis(self) -> None:
        long_title = "word " * 60  # ~300 chars
        name = build_thread_name("VEN-3", long_title, max_words=0)
        assert len(name) <= THREAD_NAME_MAX
        assert name.endswith("…")

    def test_custom_max_len(self) -> None:
        name = build_thread_name("VEN-4", "alpha beta gamma", max_len=12, max_words=0)
        assert len(name) <= 12
        assert name.endswith("…")

    def test_empty_identifier_degrades_to_title(self) -> None:
        assert build_thread_name("", "just a title") == "just a title"
        assert build_thread_name(None, "just a title") == "just a title"

    def test_both_empty_degrades_to_task(self) -> None:
        assert build_thread_name("", "") == "task"
        assert build_thread_name(None, None) == "task"

    def test_identifier_only(self) -> None:
        assert build_thread_name("VEN-9", "") == "VEN-9"


class TestResolveThreadPings:
    def test_creator_always_included(self) -> None:
        pings = resolve_thread_pings(invoker_id=111)
        assert pings.user_ids == (111,)
        assert pings.role_ids == ()

    def test_no_invoker_id_yields_no_creator(self) -> None:
        pings = resolve_thread_pings(invoker_id=None)
        assert pings.user_ids == ()

    def test_assignee_resolved_via_reverse_map(self) -> None:
        pings = resolve_thread_pings(
            invoker_id=111,
            assignee="member-uuid-7",
            reverse_member_map={"member-uuid-7": "222"},
        )
        assert pings.user_ids == (111, 222)

    def test_name_based_assignee_not_resolvable_is_skipped(self) -> None:
        pings = resolve_thread_pings(
            invoker_id=111,
            assignee="Bob the Builder",
            reverse_member_map={"member-uuid-7": "222"},
        )
        assert pings.user_ids == (111,)

    def test_assignee_strips_whitespace_before_lookup(self) -> None:
        pings = resolve_thread_pings(
            invoker_id=111,
            assignee="  member-uuid-7  ",
            reverse_member_map={"member-uuid-7": "222"},
        )
        assert pings.user_ids == (111, 222)

    def test_configured_watchers_appended(self) -> None:
        pings = resolve_thread_pings(
            invoker_id=111,
            ping_user_ids=(333, 444),
            ping_role_ids=(555,),
        )
        assert pings.user_ids == (111, 333, 444)
        assert pings.role_ids == (555,)

    def test_dedupes_creator_assignee_and_watchers(self) -> None:
        # invoker == assignee == a configured watcher: appears once.
        pings = resolve_thread_pings(
            invoker_id=111,
            assignee="member-uuid-1",
            reverse_member_map={"member-uuid-1": "111"},
            ping_user_ids=(111, 222),
        )
        assert pings.user_ids == (111, 222)

    def test_role_dedupe(self) -> None:
        pings = resolve_thread_pings(invoker_id=1, ping_role_ids=(9, 9, 8))
        assert pings.role_ids == (9, 8)

    def test_non_numeric_mapped_id_is_skipped(self) -> None:
        pings = resolve_thread_pings(
            invoker_id=111,
            assignee="x",
            reverse_member_map={"x": "not-a-number"},
        )
        assert pings.user_ids == (111,)


class TestThreadPings:
    def test_is_empty(self) -> None:
        assert ThreadPings().is_empty is True
        assert ThreadPings(user_ids=(1,)).is_empty is False
        assert ThreadPings(role_ids=(1,)).is_empty is False


class TestBuildAllowedMentions:
    def test_scopes_to_exact_ids_and_blocks_everyone(self) -> None:
        am = build_allowed_mentions(ThreadPings(user_ids=(1, 2), role_ids=(3,)))
        assert am.everyone is False
        assert isinstance(am.users, list)
        assert {o.id for o in am.users} == {1, 2}
        assert {o.id for o in am.roles} == {3}

    def test_empty_pings_mention_nobody(self) -> None:
        am = build_allowed_mentions(ThreadPings())
        assert am.everyone is False
        assert am.users == []
        assert am.roles == []


class TestBuildThreadIntro:
    def test_includes_link_priority_description_and_pings(self) -> None:
        intro = build_thread_intro(
            identifier="VEN-99",
            issue_id="uuid-1",
            title="Fix login",
            app_url="http://multica.local:3000",
            pings=ThreadPings(user_ids=(111, 222), role_ids=(333,)),
            description="The redirect drops the session cookie.",
            priority="high",
        )
        assert "VEN-99" in intro
        assert "http://multica.local:3000/venchur/issues/VEN-99" in intro
        assert "high" in intro
        assert "session cookie" in intro
        assert "<@111>" in intro and "<@222>" in intro and "<@&333>" in intro
        assert "uuid-1" not in intro  # uuid never leaks when identifier present

    def test_plain_ref_without_app_url(self) -> None:
        intro = build_thread_intro(
            identifier="VEN-7",
            issue_id="uuid-2",
            pings=ThreadPings(user_ids=(1,)),
        )
        assert "`VEN-7`" in intro
        assert "http" not in intro

    def test_uuid_used_when_no_identifier(self) -> None:
        intro = build_thread_intro(
            identifier="",
            issue_id="uuid-3",
            app_url="http://m.local",
            pings=ThreadPings(),
        )
        assert "uuid-3" in intro

    def test_description_truncated(self) -> None:
        intro = build_thread_intro(
            identifier="VEN-1",
            issue_id="i",
            pings=ThreadPings(),
            description="x" * 5000,
        )
        assert "…" in intro
        assert len(intro) < 2000  # well under Discord's 2000-char message limit

    def test_no_pings_omits_mention_line(self) -> None:
        intro = build_thread_intro(identifier="VEN-1", issue_id="i", pings=ThreadPings())
        assert "задача на вас" not in intro

    def test_markdown_in_title_is_escaped(self) -> None:
        intro = build_thread_intro(
            identifier="VEN-1",
            issue_id="i",
            title="**boom** @everyone",
            pings=ThreadPings(),
        )
        assert "\\*\\*boom\\*\\*" in intro


class _FakeThread:
    def __init__(self) -> None:
        self.send = AsyncMock()


def _msg_with_thread() -> MagicMock:
    msg = MagicMock()
    thread = _FakeThread()
    msg.create_thread = AsyncMock(return_value=thread)
    return msg


def _channel_with_thread() -> MagicMock:
    ch = MagicMock()
    thread = _FakeThread()
    ch.create_thread = AsyncMock(return_value=thread)
    return ch


def _http_error() -> discord.HTTPException:
    return discord.HTTPException(response=MagicMock(status=403, reason="Forbidden"), message="nope")


class TestOpenTaskThreadPublic:
    async def test_public_creates_thread_from_message_and_posts_intro(self) -> None:
        msg = _msg_with_thread()
        am = build_allowed_mentions(ThreadPings(user_ids=(1,)))
        thread = await open_task_thread(
            message=msg,
            channel=MagicMock(),
            name="VEN-1 do a thing",
            intro="hello <@1>",
            allowed_mentions=am,
            private=False,
            auto_archive_minutes=4320,
        )
        msg.create_thread.assert_awaited_once()
        kwargs = msg.create_thread.call_args.kwargs
        assert kwargs["name"] == "VEN-1 do a thing"
        assert kwargs["auto_archive_duration"] == 4320
        thread.send.assert_awaited_once_with("hello <@1>", allowed_mentions=am)

    async def test_public_does_not_touch_channel_create_thread(self) -> None:
        msg = _msg_with_thread()
        channel = _channel_with_thread()
        await open_task_thread(
            message=msg,
            channel=channel,
            name="n",
            intro="i",
            allowed_mentions=build_allowed_mentions(ThreadPings()),
            private=False,
        )
        channel.create_thread.assert_not_called()


class TestOpenTaskThreadPrivate:
    async def test_private_creates_standalone_thread_on_channel(self) -> None:
        channel = _channel_with_thread()
        thread = await open_task_thread(
            message=MagicMock(),
            channel=channel,
            name="VEN-2 secret",
            intro="intro",
            allowed_mentions=build_allowed_mentions(ThreadPings(user_ids=(9,))),
            private=True,
            auto_archive_minutes=1440,
        )
        channel.create_thread.assert_awaited_once()
        kwargs = channel.create_thread.call_args.kwargs
        assert kwargs["type"] == discord.ChannelType.private_thread
        assert kwargs["auto_archive_duration"] == 1440
        assert thread is not None
        thread.send.assert_awaited_once()


class TestOpenTaskThreadResilience:
    async def test_create_thread_http_error_returns_none(self) -> None:
        msg = MagicMock()
        msg.create_thread = AsyncMock(side_effect=_http_error())
        result = await open_task_thread(
            message=msg,
            channel=MagicMock(),
            name="n",
            intro="i",
            allowed_mentions=build_allowed_mentions(ThreadPings()),
        )
        assert result is None  # swallowed, never raised

    async def test_missing_create_thread_attribute_returns_none(self) -> None:
        # A message object that lacks create_thread (e.g. a DM) must not crash.
        class _NoThread:
            pass

        result = await open_task_thread(
            message=_NoThread(),
            channel=MagicMock(),
            name="n",
            intro="i",
            allowed_mentions=build_allowed_mentions(ThreadPings()),
        )
        assert result is None

    async def test_intro_send_failure_still_returns_thread(self) -> None:
        msg = _msg_with_thread()
        # A thread whose intro-post fails: creation succeeded, send raised.
        failing_thread = _FakeThread()
        failing_thread.send = AsyncMock(side_effect=_http_error())
        msg.create_thread = AsyncMock(return_value=failing_thread)
        result = await open_task_thread(
            message=msg,
            channel=MagicMock(),
            name="n",
            intro="i",
            allowed_mentions=build_allowed_mentions(ThreadPings()),
        )
        # Thread was created; only the intro post failed — still returned.
        assert result is failing_thread

    async def test_custom_logger_used(self) -> None:
        msg = MagicMock()
        msg.create_thread = AsyncMock(side_effect=_http_error())
        log = MagicMock()
        await open_task_thread(
            message=msg,
            channel=MagicMock(),
            name="n",
            intro="i",
            allowed_mentions=build_allowed_mentions(ThreadPings()),
            log=log,
        )
        log.warning.assert_called()


class _Ref:
    def __init__(self, id: str, identifier: str | None = None, title: str | None = None) -> None:
        self.id = id
        self.identifier = identifier
        self.title = title


class TestAnnounceTaskThread:
    @staticmethod
    def _msg(thread_id: int = 4242) -> MagicMock:
        thread = MagicMock()
        thread.id = thread_id
        thread.send = AsyncMock()
        msg = MagicMock()
        msg.create_thread = AsyncMock(return_value=thread)
        return msg

    async def test_disabled_is_noop(self) -> None:
        msg = self._msg()
        result = await announce_task_thread(
            message=msg,
            channel=MagicMock(),
            ref=_Ref("u1", "VEN-1"),
            fallback_title="t",
            app_url="",
            pings=ThreadPings(),
            thread_config=ThreadConfig(enabled=False),
        )
        assert result is None
        msg.create_thread.assert_not_called()

    async def test_enabled_opens_and_persists_map(self) -> None:
        msg = self._msg(thread_id=4242)
        tm = MagicMock()
        result = await announce_task_thread(
            message=msg,
            channel=MagicMock(),
            ref=_Ref("u1", "VEN-1", "Fix it"),
            fallback_title="fallback",
            app_url="http://m",
            pings=ThreadPings(user_ids=(7,)),
            thread_config=ThreadConfig(enabled=True),
            thread_map=tm,
        )
        assert result is not None
        name = msg.create_thread.call_args.kwargs["name"]
        assert name.startswith("VEN-1")
        tm.set.assert_called_once_with("u1", 4242)

    async def test_no_thread_map_skips_persist(self) -> None:
        msg = self._msg()
        # No thread_map → just opens the thread, no persistence call.
        result = await announce_task_thread(
            message=msg,
            channel=MagicMock(),
            ref=_Ref("u1", "VEN-1"),
            fallback_title="t",
            app_url="",
            pings=ThreadPings(),
            thread_config=ThreadConfig(enabled=True),
        )
        assert result is not None


class TestThreadConfigDefaults:
    def test_defaults_are_off_and_safe(self) -> None:
        cfg = ThreadConfig()
        assert cfg.enabled is False
        assert cfg.private is False
        assert cfg.auto_archive_minutes in VALID_AUTO_ARCHIVE
        assert cfg.name_max_words == 6
        assert cfg.ping_user_ids == ()
        assert cfg.ping_role_ids == ()
