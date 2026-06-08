"""Unit tests for discord_agent_secretary.sync (bidirectional comment sync)."""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_agent_secretary.backends import BackendTimeoutError
from discord_agent_secretary.sync import (
    SYNC_MARKER,
    CommentEvent,
    format_inbound_post,
    handle_thread_reply,
    is_discord_origin,
    make_thread_reply_handler,
    parse_comment_event,
    route_comment_to_thread,
)

pytestmark = pytest.mark.unit


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _comment_body(*, event: str = "comment_created", content: str = "hello") -> bytes:
    return json.dumps(
        {
            "event": event,
            "issue": {"id": "i1", "identifier": "VEN-1"},
            "comment": {"content": content, "author_name": "bob"},
        }
    ).encode("utf-8")


class _StubMap:
    def __init__(self, i2t: dict[str, int] | None = None) -> None:
        self._i2t = dict(i2t or {})
        self._t2i = {v: k for k, v in self._i2t.items()}

    def thread_for_issue(self, issue_id: str) -> int | None:
        return self._i2t.get(issue_id)

    def issue_for_thread(self, thread_id: int) -> str | None:
        return self._t2i.get(thread_id)


class _FakeChannel(discord.abc.Messageable):
    """A real Messageable subclass so `isinstance` checks in sync.py pass."""

    def __init__(self) -> None:
        self.send = AsyncMock()

    async def _get_channel(self) -> _FakeChannel:
        return self


class TestIsDiscordOrigin:
    def test_marker_detected(self) -> None:
        assert is_discord_origin(f"text {SYNC_MARKER}") is True
        assert is_discord_origin("plain text") is False
        assert is_discord_origin(None) is False


class TestParseCommentEvent:
    def test_valid(self) -> None:
        ev = parse_comment_event(_comment_body())
        assert ev == CommentEvent(issue_id="i1", identifier="VEN-1", author="bob", content="hello")

    def test_dotted_event_name_accepted(self) -> None:
        assert parse_comment_event(_comment_body(event="comment.created")) is not None

    def test_wrong_event_rejected(self) -> None:
        assert parse_comment_event(_comment_body(event="status_changed")) is None

    def test_malformed_json_rejected(self) -> None:
        assert parse_comment_event(b"{not json") is None

    def test_missing_content_rejected(self) -> None:
        body = json.dumps({"event": "comment_created", "issue": {"id": "i1"}, "comment": {}}).encode()
        assert parse_comment_event(body) is None

    def test_signature_verified(self) -> None:
        body = _comment_body()
        good = _sign(body, "s3cr3t")
        assert parse_comment_event(body, signature=good, secret="s3cr3t") is not None
        assert parse_comment_event(body, signature="sha256=bad", secret="s3cr3t") is None
        assert parse_comment_event(body, signature="", secret="s3cr3t") is None


class TestFormatInboundPost:
    def test_author_and_content(self) -> None:
        out = format_inbound_post(CommentEvent("i", "VEN-1", "alice", "the comment"))
        assert "alice" in out
        assert "the comment" in out

    def test_default_author_and_escaping(self) -> None:
        out = format_inbound_post(CommentEvent("i", "VEN-1", None, "**bold**"))
        assert "трекер" in out
        assert "\\*\\*bold\\*\\*" in out

    def test_truncation(self) -> None:
        out = format_inbound_post(CommentEvent("i", "VEN-1", "a", "x" * 5000))
        assert out.endswith("…")
        assert len(out) < 2000


def _client_with_channel(channel: object | None) -> MagicMock:
    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)
    client.fetch_channel = AsyncMock(return_value=channel)
    return client


class TestRouteCommentToThread:
    async def test_posts_to_mapped_thread(self) -> None:
        ch = _FakeChannel()
        client = _client_with_channel(ch)
        ev = CommentEvent("i1", "VEN-1", "bob", "hello from tracker")
        ok = await route_comment_to_thread(client, _StubMap({"i1": 1001}), ev)
        assert ok is True
        ch.send.assert_awaited_once()
        assert ch.send.call_args.kwargs["allowed_mentions"].everyone is False

    async def test_discord_origin_skipped(self) -> None:
        ch = _FakeChannel()
        client = _client_with_channel(ch)
        ev = CommentEvent("i1", "VEN-1", "bob", f"echo {SYNC_MARKER}")
        assert await route_comment_to_thread(client, _StubMap({"i1": 1001}), ev) is False
        ch.send.assert_not_called()

    async def test_no_mapping_skipped(self) -> None:
        ch = _FakeChannel()
        client = _client_with_channel(ch)
        ev = CommentEvent("i9", "VEN-9", "bob", "hi")
        assert await route_comment_to_thread(client, _StubMap({}), ev) is False

    async def test_fetch_when_not_cached(self) -> None:
        ch = _FakeChannel()
        client = MagicMock()
        client.get_channel = MagicMock(return_value=None)
        client.fetch_channel = AsyncMock(return_value=ch)
        ev = CommentEvent("i1", "VEN-1", "bob", "hi")
        assert await route_comment_to_thread(client, _StubMap({"i1": 1001}), ev) is True
        ch.send.assert_awaited_once()

    async def test_non_messageable_channel(self) -> None:
        client = _client_with_channel(MagicMock())  # plain mock is not Messageable
        ev = CommentEvent("i1", "VEN-1", "bob", "hi")
        assert await route_comment_to_thread(client, _StubMap({"i1": 1001}), ev) is False


def _thread_message(content: str, *, author_id: int = 7, bot: bool = False, channel_id: int = 1001) -> MagicMock:
    m = MagicMock()
    m.author.id = author_id
    m.author.bot = bot
    m.channel.id = channel_id
    m.content = content
    m.add_reaction = AsyncMock()
    return m


class TestHandleThreadReply:
    async def test_human_reply_adds_comment_with_marker(self) -> None:
        backend = MagicMock()
        backend.add_comment = AsyncMock()
        msg = _thread_message("please review this")
        ok = await handle_thread_reply(
            msg,
            client_user_id=999,
            backend=backend,
            thread_map=_StubMap({"i1": 1001}),
            member_map={"7": "member-uuid-7"},
        )
        assert ok is True
        backend.add_comment.assert_awaited_once()
        args, kwargs = backend.add_comment.call_args
        assert args[0] == "i1"
        assert SYNC_MARKER in args[1]
        assert "please review this" in args[1]
        assert kwargs["on_behalf_of"] == "member-uuid-7"
        msg.add_reaction.assert_awaited_once()

    async def test_bot_message_ignored(self) -> None:
        backend = MagicMock()
        backend.add_comment = AsyncMock()
        msg = _thread_message("x", bot=True)
        assert await handle_thread_reply(
            msg, client_user_id=999, backend=backend, thread_map=_StubMap({"i1": 1001}), member_map={}
        ) is False
        backend.add_comment.assert_not_called()

    async def test_self_message_ignored(self) -> None:
        backend = MagicMock()
        backend.add_comment = AsyncMock()
        msg = _thread_message("x", author_id=999)
        assert await handle_thread_reply(
            msg, client_user_id=999, backend=backend, thread_map=_StubMap({"i1": 1001}), member_map={}
        ) is False

    async def test_unmapped_thread_ignored(self) -> None:
        backend = MagicMock()
        backend.add_comment = AsyncMock()
        msg = _thread_message("x", channel_id=5555)
        assert await handle_thread_reply(
            msg, client_user_id=999, backend=backend, thread_map=_StubMap({"i1": 1001}), member_map={}
        ) is False

    async def test_empty_content_ignored(self) -> None:
        backend = MagicMock()
        backend.add_comment = AsyncMock()
        msg = _thread_message("   ")
        assert await handle_thread_reply(
            msg, client_user_id=999, backend=backend, thread_map=_StubMap({"i1": 1001}), member_map={}
        ) is False

    async def test_backend_error_returns_false(self) -> None:
        backend = MagicMock()
        backend.add_comment = AsyncMock(side_effect=BackendTimeoutError())
        msg = _thread_message("text")
        assert await handle_thread_reply(
            msg, client_user_id=999, backend=backend, thread_map=_StubMap({"i1": 1001}), member_map={}
        ) is False
        msg.add_reaction.assert_not_called()


class TestMakeThreadReplyHandler:
    async def test_handler_dispatches(self) -> None:
        client = MagicMock()
        client.user.id = 999
        backend = MagicMock()
        backend.add_comment = AsyncMock()
        handler = make_thread_reply_handler(
            client, backend=backend, thread_map=_StubMap({"i1": 1001}), member_map={}
        )
        await handler(_thread_message("hi from thread"))
        backend.add_comment.assert_awaited_once()
