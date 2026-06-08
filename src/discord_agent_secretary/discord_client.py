"""Discord client factory with fail-safe permission check.

The bot refuses to boot if it holds `ADMINISTRATOR` or `MANAGE_GUILD` in any
of its connected guilds. This is a defense-in-depth measure: even if invite
permissions get misconfigured, the bot will disconnect rather than run with
server-admin authority.
"""
from __future__ import annotations

import logging
import os
from typing import Final

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

REFUSE_PERMS: Final[frozenset[str]] = frozenset({
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "ban_members",
    "kick_members",
    "mention_everyone",
})


class UnsafePermissionsError(RuntimeError):
    """Raised when the bot has permissions outside the safe allow-list."""


def build_intents(*, enable_message_content: bool = False) -> discord.Intents:
    """Minimal intents — slash commands only by default.

    Presence / Server Members stay OFF unconditionally. The privileged MESSAGE
    CONTENT intent stays OFF unless `enable_message_content=True`, which the
    passive secretary observer (DISCORD_OBSERVER_ENABLED) requires to read
    message bodies. Enabling it needs the intent toggled in the Dev Portal and,
    past 100 servers, Discord verification — see docs/threat-model.md.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    if enable_message_content:
        intents.message_content = True
    return intents


def assert_safe_permissions(guild: discord.Guild, bot_member: discord.Member) -> None:
    """Verify bot permissions in a guild stay within safe bounds.

    Raises `UnsafePermissionsError` if any permission in `REFUSE_PERMS` is granted.
    Called from `on_ready` for each connected guild.

    Checks only the bot's own assigned roles — the @everyone role (which shares
    the guild ID) is excluded because its permissions are server-wide defaults that
    apply to all human members too and are not a specific grant to the bot.
    """
    own_roles = [r for r in bot_member.roles if r.id != guild.id]
    combined = discord.Permissions()
    for role in own_roles:
        combined = discord.Permissions(combined.value | role.permissions.value)
    perms = combined
    violations = sorted(p for p in REFUSE_PERMS if getattr(perms, p, False))
    if violations:
        raise UnsafePermissionsError(
            f"bot has unsafe permissions in guild {guild.id} "
            f"({guild.name!r}): {violations}. Revoke via role/channel override."
        )


def build_client(
    *, enable_message_content: bool = False
) -> tuple[discord.Client, app_commands.CommandTree]:
    """Return (client, command_tree) ready for handler registration.

    Caller adds commands via `tree.command()` decorators, then runs the client
    with `client.run(token)`. Pass `enable_message_content=True` only when the
    passive observer is enabled.
    """
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
    client = discord.Client(
        intents=build_intents(enable_message_content=enable_message_content), proxy=proxy
    )
    tree = app_commands.CommandTree(client)
    return client, tree
