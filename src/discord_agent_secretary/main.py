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
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands

from .approval_buttons import ApprovalButton
from .backends import make_backend
from .config import get_settings
from .digest_worker import DigestWorker
from .discord_client import (
    UnsafePermissionsError,
    assert_safe_permissions,
    build_client,
)
from .handlers import register_handlers
from .health import HealthcheckHandle, start_healthcheck
from .logging_setup import configure_logging
from .mention_scanner import MentionScanWorker
from .pull_worker import ReviewPollWorker
from .review_router import AutomatedReviewRouter, CliReviewBackend
from .webhook import (
    ReviewEvent,
    format_review_message,
    parse_review_event,
    should_notify_discord_for_review,
)

logger = logging.getLogger(__name__)


_SECRET_FIELDS = (
    "discord_bot_token",
    "github_token",
    "linear_api_key",
    "jira_api_token",
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
    digest_task: asyncio.Task[None] | None = None
    mention_task: asyncio.Task[None] | None = None
    review_router: AutomatedReviewRouter | None = None


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


def _log_background_failure(
    future: Future[None],
    *,
    action: str,
    issue_id: str,
    identifier: str,
    origin_type: str | None = None,
    origin_id: str | None = None,
    origin_source: str | None = None,
    routing_outcome: str | None = None,
) -> None:
    try:
        future.result()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s failed",
            action,
            extra={
                "issue_id": issue_id,
                "identifier": identifier,
                "origin_type": origin_type,
                "origin_id": origin_id,
                "origin_source": origin_source,
                "routing_outcome": routing_outcome,
                "detail": str(exc),
            },
        )


def _review_log_extra(
    event: ReviewEvent,
    *,
    routing_outcome: str | None = None,
    reviewer_ref: str | None = None,
) -> dict[str, object]:
    return {
        "issue_id": event.issue_id,
        "identifier": event.identifier,
        "origin_type": event.origin_type,
        "origin_id": event.origin_id,
        "origin_source": event.origin_source,
        "routing_outcome": routing_outcome,
        "reviewer_ref": reviewer_ref,
    }


def _issue_from_review_event(event: ReviewEvent) -> dict[str, object]:
    return {
        "id": event.issue_id,
        "identifier": event.identifier,
        "title": event.title,
        "assignee_type": event.assignee_type or "agent",
        "assignee_id": event.assignee_id,
        "origin_type": event.origin_type,
        "origin_id": event.origin_id,
        "origin_source": event.origin_source,
        "status": "in_review",
    }


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
        if not should_notify_discord_for_review(event):
            router = state.review_router
            if router is None:
                logger.info(
                    "review notification suppressed",
                    extra=_review_log_extra(event, routing_outcome="routing_unconfigured"),
                )
                return
            if loop is None:
                logger.warning(
                    "review routing dropped: event loop not ready",
                    extra=_review_log_extra(event, routing_outcome="loop_unavailable"),
                )
                return

            async def _route_review() -> None:
                result = await router.route_issue(_issue_from_review_event(event))
                logger.info(
                    "review notification suppressed",
                    extra=_review_log_extra(
                        event,
                        routing_outcome=result.outcome,
                        reviewer_ref=result.reviewer_ref,
                    ),
                )

            future = asyncio.run_coroutine_threadsafe(_route_review(), loop)
            future.add_done_callback(
                lambda f: _log_background_failure(
                    f,
                    action="review routing",
                    issue_id=event.issue_id,
                    identifier=event.identifier,
                    origin_type=event.origin_type,
                    origin_id=event.origin_id,
                    origin_source=event.origin_source,
                    routing_outcome="routing_failed",
                )
            )
            return
        if loop is None:
            logger.warning("review notification dropped: event loop not ready")
            return
        future = asyncio.run_coroutine_threadsafe(
            _send_review_notification(client, channel_id, format_review_message(event)),
            loop,
        )
        future.add_done_callback(
            lambda f: _log_background_failure(
                f,
                action="review notification",
                issue_id=event.issue_id,
                identifier=event.identifier,
                origin_type=event.origin_type,
                origin_id=event.origin_id,
                origin_source=event.origin_source,
            )
        )

    return _on_webhook


def _log_worker_exit(task: asyncio.Task) -> None:
    """A background worker should run forever; an unexpected exit is critical."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.critical(
            "background worker exited unexpectedly — no longer running",
            extra={"task": task.get_name(), "detail": str(exc)},
        )


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
    # Persistent approval buttons: register the dynamic item so clicks are routed
    # by custom_id even after a restart (no in-memory View needed). Gate only on
    # the member map — the callback authorizes by member, and buttons posted in a
    # prior run must keep working even if mention-scan is later toggled off.
    if settings.discord_member_map:
        client.add_dynamic_items(ApprovalButton)
    register_handlers(
        tree,
        backend,
        settings.discord_guild_id,
        app_url=settings.multica_app_url,
        member_map=settings.discord_member_map,
    )

    state = _RunState()

    review_router: AutomatedReviewRouter | None = None
    if settings.multica_review_routing_mode != "off":
        review_router = AutomatedReviewRouter(
            reviewer_refs=settings.multica_automated_reviewers,
            routing_mode=settings.multica_review_routing_mode,
            rework_status=settings.multica_rework_status,
            dry_run=settings.multica_review_dry_run,
            state_path=Path(settings.multica_review_state_path),
            backend=CliReviewBackend(
                settings.multica_cli_path or "multica",
                timeout=max(settings.multica_cli_timeout, 30.0),
            ),
        )
        state.review_router = review_router
        logger.info(
            "automated review router configured",
            extra={
                "mode": settings.multica_review_routing_mode,
                "dry_run": settings.multica_review_dry_run,
                "reviewer_count": len(settings.multica_automated_reviewers),
                "state_path": settings.multica_review_state_path,
            },
        )
        if (
            settings.discord_review_channel_id is not None
            and settings.multica_review_dry_run
            and not settings.multica_webhook_secret.strip()
        ):
            # Hard-required once dry_run is off (see config validator); warn while
            # dry_run masks that gap so the secret is set before going live.
            logger.warning(
                "review webhook accepts UNSIGNED payloads (dry_run + no "
                "MULTICA_WEBHOOK_SECRET) — set the secret before disabling dry_run"
            )

    webhook_cb = _make_webhook_callback(settings, client, state)

    poll_worker: ReviewPollWorker | None = None
    if settings.discord_review_channel_id:
        poll_worker = ReviewPollWorker(
            cli_path=settings.multica_cli_path or "multica",
            channel_id=settings.discord_review_channel_id,
            seen_path=Path(settings.multica_seen_path),
            poll_interval=settings.multica_poll_interval,
            app_url=settings.multica_app_url,
            review_router=review_router,
            cli_timeout=max(settings.multica_cli_timeout, 30.0),
            failure_alert_threshold=settings.backend_circuit_failure_threshold,
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

    digest_worker: DigestWorker | None = None
    if settings.discord_review_channel_id and settings.digest_enabled:
        digest_worker = DigestWorker(
            cli_path=settings.multica_cli_path or "multica",
            channel_id=settings.discord_review_channel_id,
            app_url=settings.multica_app_url,
            tz=settings.tz,
            digest_hour=settings.digest_hour,
            state_path=Path(settings.digest_state_path),
            cli_timeout=max(settings.multica_cli_timeout, 30.0),
            failure_alert_threshold=settings.backend_circuit_failure_threshold,
        )
        logger.info(
            "autopilot digest configured",
            extra={"channel_id": settings.discord_review_channel_id, "hour": settings.digest_hour},
        )

    mention_worker: MentionScanWorker | None = None
    if settings.discord_review_channel_id and settings.mention_scan_enabled and settings.discord_member_map:
        mention_worker = MentionScanWorker(
            cli_path=settings.multica_cli_path or "multica",
            channel_id=settings.discord_review_channel_id,
            app_url=settings.multica_app_url,
            statuses=settings.mention_scan_statuses,
            discord_member_map=settings.discord_member_map,
            state_path=Path(settings.mention_scan_state_path),
            poll_interval=settings.multica_poll_interval,
            cli_timeout=max(settings.multica_cli_timeout, 30.0),
            member_map_ttl=settings.mention_member_map_ttl,
            failure_alert_threshold=settings.backend_circuit_failure_threshold,
        )
        logger.info(
            "mention scanner configured",
            extra={"channel_id": settings.discord_review_channel_id, "statuses": settings.mention_scan_statuses},
        )

    def _liveness() -> dict[str, str]:
        out: dict[str, str] = {}
        if poll_worker and poll_worker.last_poll_ok:
            out["review-poll"] = poll_worker.last_poll_ok
        if digest_worker and digest_worker.last_cycle_ok:
            out["digest"] = digest_worker.last_cycle_ok
        if mention_worker and mention_worker.last_cycle_ok:
            out["mention-scan"] = mention_worker.last_cycle_ok
        return out

    health_handle = start_healthcheck(
        settings.healthcheck_port,
        is_ready=client.is_ready,
        webhook_callback=webhook_cb,
        liveness=_liveness,
        rate_limit=settings.webhook_rate_limit,
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
            state.poll_task.add_done_callback(_log_worker_exit)
        if digest_worker is not None and state.digest_task is None:
            state.digest_task = asyncio.create_task(
                digest_worker.run(client),
                name="autopilot-digest-worker",
            )
            state.digest_task.add_done_callback(_log_worker_exit)
        if mention_worker is not None and state.mention_task is None:
            state.mention_task = asyncio.create_task(
                mention_worker.run(client),
                name="mention-scan-worker",
            )
            state.mention_task.add_done_callback(_log_worker_exit)

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
