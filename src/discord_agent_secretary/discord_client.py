"""Discord client factory with fail-safe permission check.

The bot refuses to boot if it holds `ADMINISTRATOR` or `MANAGE_GUILD` in any
of its connected guilds. This is a defense-in-depth measure: even if invite
permissions get misconfigured, the bot will disconnect rather than run with
server-admin authority.
"""
from __future__ import annotations

import logging
from typing import Final

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

REFUSE_PERMS: Final[frozenset[str]] = frozenset({
    "administrator",
    "manage_guild",
})


class UnsafePermissionsError(RuntimeError):
    """Raised when the bot has permissions outside the safe allow-list."""


def build_intents() -> discord.Intents:
    """Minimal intents — slash commands only.

    Privileged intents (Presence / Server Members / Message Content) stay OFF
    to match the security plan. If message-content reading becomes needed for
    the secretary mode (P5), enable explicitly and request verification.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    return intents


def assert_safe_permissions(guild: discord.Guild, bot_member: discord.Member) -> None:
    """Verify bot permissions in a guild stay within safe bounds.

    Raises `UnsafePermissionsError` if any permission in `REFUSE_PERMS` is granted.
    Called from `on_ready` for each connected guild.
    """
    perms = bot_member.guild_permissions
    violations = sorted(p for p in REFUSE_PERMS if getattr(perms, p, False))
    if violations:
        raise UnsafePermissionsError(
            f"bot has unsafe permissions in guild {guild.id} "
            f"({guild.name!r}): {violations}. Revoke via role/channel override."
        )


def build_client() -> tuple[discord.Client, app_commands.CommandTree]:
    """Return (client, command_tree) ready for handler registration.

    Caller adds commands via `tree.command()` decorators, then runs the client
    with `client.run(token)`.
    """
    client = discord.Client(intents=build_intents())
    tree = app_commands.CommandTree(client)
    return client, tree
