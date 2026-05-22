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
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands

from .backends import make_backend
from .config import get_settings
from .discord_client import (
    UnsafePermissionsError,
    assert_safe_permissions,
    build_client,
)
from .handlers import register_handlers
from .health import HealthcheckHandle, start_healthcheck
from .logging_setup import configure_logging
from .pull_worker import ReviewPollWorker
from .webhook import format_review_message, parse_review_event

logger = logging.getLogger(__name__)


_SECRET_FIELDS = (
    "discord_bot_token",
    "github_token",
    "linear_api_key",
    "jira_api_token",
    "anthropic_api_key",
    "multica_webhook_secret",
)


def _collect_secrets(settings: object) -> list[str]:
    """Pull every secret-shaped setting for the log redactor.

    Strings only; empties stay in — the filter drops anything below its
    minimum length.
    """
    candidates: list[str] = []
    for attr in _SECRET_FIELDS:
        value = getattr(settings, attr, "")
        if isinstance(value, str):
            candidates.append(value)
    return candidates


@dataclass
class _RunState:
    """Mutable lifecycle flags shared with the on_ready callback.

    `aborted` surfaces a non-zero exit when the bot refuses to run;
    `synced` keeps tree.sync() from re-firing on every reconnect — Discord
    rate-limits global sync to 200/day.
    """

    aborted: bool = False
    synced: bool = False
    loop: asyncio.AbstractEventLoop | None = None
    poll_worker: ReviewPollWorker | None = None
    poll_task: asyncio.Task[None] | None = None


async def resolve_bot_member(
    guild: discord.Guild,
    bot_user_id: int | None,
) -> discord.Member | None:
    """Return the bot's own Member in `guild` — cache first, then REST.

    Cache misses fall through to a one-shot `fetch_member`. `None` means
    membership is unresolvable and the caller should refuse to run rather
    than operate with unknown authority.
    """
    if bot_user_id is None:
        return None
    member = guild.get_member(bot_user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(bot_user_id)
    except discord.HTTPException as e:
        logger.warning(
            "fetch_member failed",
            extra={"guild_id": guild.id, "detail": str(e)},
        )
        return None


async def verify_guilds_safe(client: discord.Client) -> bool:
    """Verify bot permissions in every connected guild.

    Always fetches member data via REST — never the local cache — so the
    permission check uses Discord's authoritative current state regardless
    of what the GUILD_CREATE event cached locally.

    Returns False (and logs CRITICAL) on the first unsafe guild or
    unresolvable membership. Caller must abort if False.
    """
    user = client.user
    bot_user_id = user.id if user else None
    if bot_user_id is None:
        return True
    for guild in client.guilds:
        try:
            bot_member = await guild.fetch_member(bot_user_id)
        except discord.HTTPException as exc:
            logger.critical(
                "refusing to run: bot membership unresolved",
                extra={"guild_id": guild.id, "guild_name": guild.name, "detail": str(exc)},
            )
            return False
        try:
            assert_safe_permissions(guild, bot_member)
        except UnsafePermissionsError as e:
            logger.critical("refusing to run: %s", e)
            return False
    return True


async def sync_commands(
    tree: app_commands.CommandTree,
    guild_id: int | None,
) -> int:
    """Sync slash commands and return the count synced."""
    if guild_id:
        guild_obj = discord.Object(id=guild_id)
        synced = await tree.sync(guild=guild_obj)
        logger.info(
            "slash commands synced (guild-scoped)",
            extra={"guild_id": guild_id, "count": len(synced)},
        )
    else:
        synced = await tree.sync()
        logger.info("slash commands synced (global)", extra={"count": len(synced)})
    return len(synced)


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    callback: Callable[[], None],
) -> list[signal.Signals]:
    """Register SIGTERM/SIGINT handlers; return the ones that took.

    Windows / non-main-thread can't add signal handlers via asyncio — we
    log and let discord.py's own handlers stand in.
    """
    registered: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, callback)
            registered.append(sig)
        except (NotImplementedError, RuntimeError) as exc:
            logger.warning(
                "signal handler not registered — graceful shutdown may not work",
                extra={"signal": sig.name, "detail": str(exc)},
            )
    return registered


async def run_client(client: discord.Client, token: str) -> None:
    """Run the client with graceful-close signal handlers.

    The shutdown task is held in a strong-reference set until done — without
    it, `loop.create_task(...)` may be GC'd mid-flight on 3.11+ (PEP 3156).
    """
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()

    def _request_close() -> None:
        logger.info("shutdown signal received — closing Discord client")
        task = loop.create_task(client.close())
        pending.add(task)
        task.add_done_callback(pending.discard)

    install_signal_handlers(loop, _request_close)

    async with client:
        await client.start(token)


def _shutdown_healthcheck(handle: HealthcheckHandle | None) -> None:
    """Close the healthcheck server, swallowing OSError so the main exit
    code reflects the bot lifecycle, not socket teardown noise."""
    if handle is None:
        return
    try:
        handle.shutdown()
    except OSError as exc:
        logger.warning("health server shutdown raised", extra={"detail": str(exc)})


async def _send_review_notification(
    client: discord.Client, channel_id: int, message: str
) -> None:
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException as e:
            logger.warning(
                "review channel unavailable",
                extra={"channel_id": channel_id, "detail": str(e)},
            )
            return
    if isinstance(channel, discord.abc.Messageable):
        await channel.send(message)


def _make_webhook_callback(
    settings: object, client: discord.Client, state: _RunState
) -> Callable[[bytes, str], None] | None:
    discord_review_channel_id = getattr(settings, "discord_review_channel_id", None)
    if not discord_review_channel_id:
        return None
    multica_webhook_secret = getattr(settings, "multica_webhook_secret", "")
    channel_id = discord_review_channel_id

    def _on_webhook(body: bytes, signature: str) -> None:
        event = parse_review_event(body, signature=signature, secret=multica_webhook_secret)
        if event is None:
            return
        loop = state.loop
        if loop is None:
            logger.warning("review notification dropped: event loop not ready")
            return
        asyncio.run_coroutine_threadsafe(
            _send_review_notification(client, channel_id, format_review_message(event)),
            loop,
        )

    return _on_webhook


def main() -> int:
    try:
        settings = get_settings()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(f"CRITICAL: configuration failed: {exc}", file=sys.stderr)
        return 1
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

    state = _RunState()

    webhook_cb = _make_webhook_callback(settings, client, state)

    poll_worker: ReviewPollWorker | None = None
    if settings.discord_review_channel_id:
        poll_worker = ReviewPollWorker(
            cli_path=settings.multica_cli_path or "multica",
            channel_id=settings.discord_review_channel_id,
            seen_path=Path(settings.multica_seen_path),
            poll_interval=settings.multica_poll_interval,
            app_url=settings.multica_app_url,
            cli_timeout=max(settings.multica_cli_timeout, 30.0),
        )
        state.poll_worker = poll_worker
        logger.info(
            "review pull poller configured",
            extra={
                "channel_id": settings.discord_review_channel_id,
                "interval": settings.multica_poll_interval,
                "seen_path": settings.multica_seen_path,
            },
        )

    health_handle = start_healthcheck(
        settings.healthcheck_port,
        is_ready=client.is_ready,
        webhook_callback=webhook_cb,
        poll_state=(lambda: poll_worker.last_poll_ok) if poll_worker else None,
    )

    @client.event
    async def on_ready() -> None:
        user = client.user
        logger.info(
            "bot connected",
            extra={"bot_user": str(user), "bot_id": user.id if user else None},
        )
        state.loop = asyncio.get_running_loop()
        if not await verify_guilds_safe(client):
            state.aborted = True
            await client.close()
            return
        if state.synced:
            return
        await sync_commands(tree, settings.discord_guild_id)
        state.synced = True
        if poll_worker is not None and state.poll_task is None:
            state.poll_task = asyncio.create_task(
                poll_worker.run(client),
                name="review-poll-worker",
            )

    try:
        asyncio.run(run_client(client, settings.discord_bot_token))
    except discord.LoginFailure:
        logger.critical("Discord rejected token — check DISCORD_BOT_TOKEN")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        _shutdown_healthcheck(health_handle)

    return 1 if state.aborted else 0


if __name__ == "__main__":
    sys.exit(main())
