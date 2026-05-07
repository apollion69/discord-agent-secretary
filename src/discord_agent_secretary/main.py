"""discord-agent-secretary entrypoint.

Usage:
    python -m discord_agent_secretary       # dev
    discord-agent-secretary                  # via pyproject script entry
"""
from __future__ import annotations

import logging
import sys

import discord

from .backends import make_backend
from .config import get_settings
from .discord_client import (
    UnsafePermissionsError,
    assert_safe_permissions,
    build_client,
)
from .handlers import register_handlers
from .logging_setup import configure_logging

logger = logging.getLogger(__name__)


def main() -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    if not settings.discord_bot_token:
        logger.critical("DISCORD_BOT_TOKEN missing — set it in .env or environment")
        return 1

    backend = make_backend(settings)

    client, tree = build_client()
    register_handlers(tree, backend, settings.discord_guild_id)

    @client.event
    async def on_ready() -> None:
        user = client.user
        logger.info(
            "bot connected",
            extra={"bot_user": str(user), "bot_id": user.id if user else None},
        )
        for guild in client.guilds:
            bot_member = guild.get_member(user.id) if user else None
            if bot_member is None:
                logger.warning(
                    "bot membership unresolved",
                    extra={"guild_id": guild.id, "guild_name": guild.name},
                )
                continue
            try:
                assert_safe_permissions(guild, bot_member)
            except UnsafePermissionsError as e:
                logger.critical("refusing to run: %s", e)
                await client.close()
                return
        if settings.discord_guild_id:
            guild_obj = discord.Object(id=settings.discord_guild_id)
            synced = await tree.sync(guild=guild_obj)
            logger.info(
                "slash commands synced (guild-scoped)",
                extra={"guild_id": settings.discord_guild_id, "count": len(synced)},
            )
        else:
            synced = await tree.sync()
            logger.info("slash commands synced (global)", extra={"count": len(synced)})

    try:
        client.run(settings.discord_bot_token, log_handler=None)
    except discord.LoginFailure:
        logger.critical("Discord rejected token — check DISCORD_BOT_TOKEN")
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
