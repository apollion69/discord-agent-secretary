"""discord-agent-secretary entrypoint.

Usage:
    python -m discord_agent_secretary       # dev
    discord-agent-secretary                  # via pyproject script entry
"""
from __future__ import annotations

import asyncio
import logging
import signal
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


def _collect_secrets(settings: object) -> list[str]:
    """Pull every secret-shaped setting for the log redactor.

    Strings only; empties stay in — the filter drops anything below its
    minimum length.
    """
    candidates: list[str] = []
    for attr in (
        "discord_bot_token",
        "github_token",
        "linear_api_key",
        "jira_api_token",
        "anthropic_api_key",
    ):
        value = getattr(settings, attr, "")
        if isinstance(value, str):
            candidates.append(value)
    return candidates


def main() -> int:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        secrets=_collect_secrets(settings),
    )

    if not settings.discord_bot_token:
        logger.critical("DISCORD_BOT_TOKEN missing — set it in .env or environment")
        return 1

    backend = make_backend(settings)

    client, tree = build_client()
    register_handlers(tree, backend, settings.discord_guild_id)

    # Mutable state shared with the on_ready closure. `aborted` lets us
    # surface a non-zero exit when the bot refuses to run; `synced` keeps
    # tree.sync() from re-firing on every reconnect (Discord rate-limits
    # global sync to 200/day).
    state = {"aborted": False, "synced": False}

    @client.event
    async def on_ready() -> None:
        user = client.user
        logger.info(
            "bot connected",
            extra={"bot_user": str(user), "bot_id": user.id if user else None},
        )
        for guild in client.guilds:
            bot_member = guild.get_member(user.id) if user else None
            if bot_member is None and user is not None:
                # Cache miss — try a one-shot REST fetch before refusing.
                try:
                    bot_member = await guild.fetch_member(user.id)
                except discord.HTTPException as e:
                    logger.warning(
                        "fetch_member failed",
                        extra={"guild_id": guild.id, "detail": str(e)},
                    )
            if bot_member is None:
                # Refuse to start in any guild where we can't verify perms.
                # Better a hard abort than running with unknown authority.
                logger.critical(
                    "refusing to run: bot membership unresolved",
                    extra={"guild_id": guild.id, "guild_name": guild.name},
                )
                state["aborted"] = True
                await client.close()
                return
            try:
                assert_safe_permissions(guild, bot_member)
            except UnsafePermissionsError as e:
                logger.critical("refusing to run: %s", e)
                state["aborted"] = True
                await client.close()
                return
        if state["synced"]:
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
        state["synced"] = True

    async def _runner() -> None:
        loop = asyncio.get_running_loop()

        def _request_close() -> None:
            logger.info("shutdown signal received — closing Discord client")
            loop.create_task(client.close())

        # SIGTERM matters in container/systemd deployments; SIGINT is also
        # registered so Ctrl+C performs a graceful close instead of leaving
        # the gateway session lingering on Discord's side.
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_close)
            except (NotImplementedError, RuntimeError):
                # Windows / non-main-thread — discord.py's own handlers stay.
                pass

        async with client:
            await client.start(settings.discord_bot_token)

    try:
        asyncio.run(_runner())
    except discord.LoginFailure:
        logger.critical("Discord rejected token — check DISCORD_BOT_TOKEN")
        return 1
    except KeyboardInterrupt:
        return 0

    return 1 if state["aborted"] else 0


if __name__ == "__main__":
    sys.exit(main())
