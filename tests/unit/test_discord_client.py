"""Unit tests for discord_agent_secretary.discord_client.

Covers intents and the fail-safe permission check.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from discord_agent_secretary.discord_client import (
    REFUSE_PERMS,
    UnsafePermissionsError,
    assert_safe_permissions,
    build_client,
    build_intents,
)

pytestmark = pytest.mark.unit


def _make_member(
    *,
    administrator: bool = False,
    manage_guild: bool = False,
    manage_roles: bool = False,
    manage_channels: bool = False,
    manage_webhooks: bool = False,
    ban_members: bool = False,
    kick_members: bool = False,
    mention_everyone: bool = False,
    view_channel: bool = True,
    send_messages: bool = True,
) -> MagicMock:
    """Build a mock `discord.Member` whose `guild_permissions` mirrors inputs."""
    perms = MagicMock(spec=discord.Permissions)
    perms.administrator = administrator
    perms.manage_guild = manage_guild
    perms.manage_roles = manage_roles
    perms.manage_channels = manage_channels
    perms.manage_webhooks = manage_webhooks
    perms.ban_members = ban_members
    perms.kick_members = kick_members
    perms.mention_everyone = mention_everyone
    perms.view_channel = view_channel
    perms.send_messages = send_messages
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = perms
    return member


def _make_guild(guild_id: int = 123, name: str = "test-guild") -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = name
    return guild


class TestBuildIntents:
    def test_privileged_intents_off(self) -> None:
        intents = build_intents()
        assert intents.message_content is False
        assert intents.members is False
        assert intents.presences is False

    def test_guild_intents_on(self) -> None:
        intents = build_intents()
        assert intents.guilds is True
        assert intents.guild_messages is True


class TestAssertSafePermissions:
    def test_allows_minimal_perms(self) -> None:
        guild = _make_guild()
        member = _make_member(view_channel=True, send_messages=True)
        assert_safe_permissions(guild, member)

    def test_blocks_administrator(self) -> None:
        guild = _make_guild(guild_id=42, name="danger")
        member = _make_member(administrator=True)
        with pytest.raises(UnsafePermissionsError) as exc:
            assert_safe_permissions(guild, member)
        assert "administrator" in str(exc.value).lower()
        assert "42" in str(exc.value)

    def test_blocks_manage_guild(self) -> None:
        guild = _make_guild()
        member = _make_member(manage_guild=True)
        with pytest.raises(UnsafePermissionsError):
            assert_safe_permissions(guild, member)

    def test_blocks_both_and_lists_them(self) -> None:
        guild = _make_guild()
        member = _make_member(administrator=True, manage_guild=True)
        with pytest.raises(UnsafePermissionsError) as exc:
            assert_safe_permissions(guild, member)
        msg = str(exc.value).lower()
        assert "administrator" in msg
        assert "manage_guild" in msg

    def test_refuse_set_is_canonical(self) -> None:
        assert REFUSE_PERMS == frozenset({
            "administrator",
            "manage_guild",
            "manage_roles",
            "manage_channels",
            "manage_webhooks",
            "ban_members",
            "kick_members",
            "mention_everyone",
        })

    @pytest.mark.parametrize(
        "perm",
        ["manage_roles", "manage_channels", "manage_webhooks",
         "ban_members", "kick_members", "mention_everyone"],
    )
    def test_blocks_each_dangerous_perm(self, perm: str) -> None:
        guild = _make_guild()
        member = _make_member(**{perm: True})
        with pytest.raises(UnsafePermissionsError) as exc:
            assert_safe_permissions(guild, member)
        assert perm in str(exc.value).lower()


class TestBuildClient:
    def test_returns_client_and_tree(self) -> None:
        client, tree = build_client()
        assert isinstance(client, discord.Client)
        assert tree is not None
        assert tree.client is client
