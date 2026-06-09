"""End-to-end flow tests wiring the real app objects together.

These exercise full chains with Discord I/O mocked at the edge:
  * `/task` slash → card + thread + persisted issue↔thread map
  * passive observer → ✅ confirm → create + thread + map
  * bidirectional sync round-trip with the echo-loop guard

Marked `e2e` (deselected by default; run with `pytest -m e2e`).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_agent_secretary.backends import IssueRef
from discord_agent_secretary.discord_client import build_client
from discord_agent_secretary.handlers import register_handlers
from discord_agent_secretary.observer import (
    TaskConfirmView,
    handle_observed_message,
)
from discord_agent_secretary.sync import (
    SYNC_MARKER,
    CommentEvent,
    handle_thread_reply,
    route_comment_to_thread,
)
from discord_agent_secretary.thread_map import ThreadMap
from discord_agent_secretary.threads import ThreadConfig

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _interaction(user_id: int = 7, guild_id: int = 42) -> MagicMock:
    interaction = MagicMock()
    interaction.id = 1
    interaction.user.id = user_id
    interaction.guild.id = guild_id
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    thread = MagicMock()
    thread.id = 50500
    thread.send = AsyncMock()
    msg = MagicMock()
    msg.create_thread = AsyncMock(return_value=thread)
    interaction.followup.send = AsyncMock(return_value=msg)
    interaction.channel = MagicMock()
    return interaction


class TestSlashTaskFullChain:
    async def test_task_creates_card_thread_and_persists_map(self, tmp_path) -> None:
        client, tree = build_client()
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="uuid-1", title="Fix login", identifier="VEN-100")
        )
        tm = ThreadMap(tmp_path / "tm.json")
        register_handlers(
            tree,
            backend,
            guild_id=42,
            app_url="http://m.local:3000",
            member_map={"999": "member-uuid-7"},
            thread_config=ThreadConfig(enabled=True, ping_user_ids=(555,)),
            cards_enabled=True,
            thread_map=tm,
        )
        cmd = tree.get_command("task", guild=discord.Object(id=42))
        assert cmd is not None
        interaction = _interaction()
        await cmd.callback(interaction, title="Fix login", assignee="member-uuid-7")

        # Card sent (Components V2 view), thread opened, map persisted.
        assert isinstance(interaction.followup.send.call_args.kwargs.get("view"), discord.ui.LayoutView)
        msg = interaction.followup.send.return_value
        msg.create_thread.assert_awaited_once()
        thread = msg.create_thread.return_value
        intro = thread.send.call_args.args[0]
        assert "<@7>" in intro  # creator
        assert "<@999>" in intro  # assignee resolved via member map
        assert "<@555>" in intro  # configured watcher
        assert tm.thread_for_issue("uuid-1") == 50500
        assert tm.issue_for_thread(50500) == "uuid-1"


class TestObserverFullChain:
    async def test_message_to_confirm_creates_task_and_map(self, tmp_path) -> None:
        backend = MagicMock()
        backend.create_issue = AsyncMock(
            return_value=IssueRef(id="uuid-2", title="Do thing", identifier="VEN-101")
        )
        tm = ThreadMap(tmp_path / "tm.json")
        msg = MagicMock()
        msg.author.id = 7
        msg.author.bot = False
        msg.channel.id = 100
        msg.content = "/task do thing [P1]"
        msg.reply = AsyncMock()
        view = await handle_observed_message(
            msg,
            client_user_id=999,
            backend=backend,
            watch_channels={100},
            triggers=("/task",),
            app_url="",
            member_map={},
            reverse_member_map={},
            thread_config=ThreadConfig(enabled=True),
            thread_map=tm,
        )
        assert isinstance(view, TaskConfirmView)
        msg.reply.assert_awaited_once()

        # Press ✅ — the observer opens the thread on interaction.message.
        interaction = MagicMock()
        interaction.user.id = 7
        interaction.response.edit_message = AsyncMock()
        thread = MagicMock()
        thread.id = 50500
        thread.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.create_thread = AsyncMock(return_value=thread)
        interaction.channel = MagicMock()
        await view._on_confirm(interaction)
        backend.create_issue.assert_awaited_once()
        assert backend.create_issue.call_args.kwargs["priority"] == "high"  # [P1] parsed
        interaction.message.create_thread.assert_awaited_once()
        assert tm.thread_for_issue("uuid-2") == 50500


class _FakeChannel(discord.abc.Messageable):
    def __init__(self) -> None:
        self.send = AsyncMock()

    async def _get_channel(self) -> _FakeChannel:
        return self


class TestSyncRoundTripWithEchoGuard:
    async def test_reply_to_tracker_and_no_echo(self) -> None:
        tm = ThreadMap.__new__(ThreadMap)  # in-memory only, no file
        tm._issue_to_thread = {"i1": 1001}
        tm._thread_to_issue = {1001: "i1"}

        backend = MagicMock()
        backend.add_comment = AsyncMock()

        # Outbound: a human reply in the thread → tracker comment with marker.
        reply = MagicMock()
        reply.author.id = 7
        reply.author.bot = False
        reply.channel.id = 1001
        reply.content = "looks good, ship it"
        reply.add_reaction = AsyncMock()
        assert await handle_thread_reply(
            reply, client_user_id=999, backend=backend, thread_map=tm, member_map={}
        ) is True
        posted = backend.add_comment.call_args.args[1]
        assert SYNC_MARKER in posted

        # Inbound echo guard: that same Discord-origin comment must NOT re-post.
        client = MagicMock()
        ch = _FakeChannel()
        client.get_channel = MagicMock(return_value=ch)
        echoed = CommentEvent("i1", "VEN-1", "bob", posted)
        assert await route_comment_to_thread(client, tm, echoed) is False
        ch.send.assert_not_called()

        # A genuine tracker-origin comment DOES post into the thread.
        genuine = CommentEvent("i1", "VEN-1", "alice", "tracker says hi")
        assert await route_comment_to_thread(client, tm, genuine) is True
        ch.send.assert_awaited_once()
