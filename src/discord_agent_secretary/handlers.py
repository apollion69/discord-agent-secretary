"""Slash-command handlers bridging Discord to a pluggable issue backend.

Each handler:
  1. Applies a per-(guild, user) token-bucket rate limit before any backend work.
  2. Defers the Discord response (backend calls can take several seconds).
  3. Invokes the injected `IssueBackend` (see `backends/base.py`).
  4. Replies with outcome or a sanitized error — raw backend stderr never
     leaks to the user-visible channel; error replies are ephemeral so they
     don't broadcast backend health to passive observers.

Handlers are backend-agnostic: they catch `BackendTimeoutError`, `BackendCallError`,
`BackendError` from the abstract hierarchy. Concrete backends raise their own
subclasses for log richness.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine, Hashable
from typing import Any, Final, TypeVar

import discord
from discord import app_commands

from .backends import (
    BackendCallError,
    BackendError,
    BackendTimeoutError,
    CircuitOpenError,
    IssueBackend,
)
from .cards import build_task_card
from .threads import (
    ThreadConfig,
    announce_task_thread,
    resolve_thread_pings,
)

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

ALLOWED_STATUSES: Final[tuple[str, ...]] = (
    "todo", "in_progress", "in_review", "done", "blocked",
)
ALLOWED_PRIORITIES: Final[tuple[str, ...]] = (
    "low", "medium", "high", "urgent",
)

_USER_FAIL: Final[str] = (
    "Что-то пошло не так при обращении к трекеру. Детали в логах; "
    "попробуй ещё раз или напиши админу бота."
)
_USER_TIMEOUT: Final[str] = "⏱️ Бэкенд не ответил вовремя. Попробуй ещё раз."
_USER_RATE_LIMITED: Final[str] = (
    "🛑 Слишком часто. Попробуй ещё раз через несколько секунд."
)
_USER_CIRCUIT_OPEN: Final[str] = (
    "⚡ Трекер сейчас недоступен — повторим попытки автоматически чуть позже. "
    "Ничего делать не нужно."
)


class RateLimiter:
    """Tiny token-bucket rate limiter, keyed by anything hashable.

    Default budget — 5 commands burst, refill 1 token / 2 s — keeps the
    backend safe from a single member spamming `/task` while leaving normal
    interactive use unaffected. State is in-process; for multi-replica
    deployments swap in a shared store (Redis, Memcached).

    Stale buckets are evicted every `_EVICT_EVERY` calls to prevent
    unbounded memory growth on long-running bots with many unique users.
    """

    _EVICT_EVERY = 1_000
    _BUCKET_TTL = 60.0 * 60 * 24  # 24 h idle → evict

    def __init__(
        self,
        capacity: int = 5,
        refill_per_sec: float = 0.5,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = capacity
        self.refill = refill_per_sec
        self._clock = clock
        self._buckets: dict[Hashable, tuple[float, float]] = {}
        self._calls = 0

    def _evict(self, now: float) -> None:
        cutoff = now - self._BUCKET_TTL
        self._buckets = {k: v for k, v in self._buckets.items() if v[1] >= cutoff}

    def acquire(self, key: Hashable, cost: float = 1.0) -> bool:
        now = self._clock()
        self._calls += 1
        if self._calls % self._EVICT_EVERY == 0:
            self._evict(now)
        tokens, last = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill)
        if tokens < cost:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - cost, now)
        return True


def _ctx_extra(interaction: discord.Interaction) -> dict[str, Any]:
    """Build the correlation-ID payload for log records."""
    return {
        "interaction_id": str(getattr(interaction, "id", "")),
        "user_id": getattr(getattr(interaction, "user", None), "id", None),
        "guild_id": getattr(getattr(interaction, "guild", None), "id", None),
    }


async def _safe_followup(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    view: discord.ui.LayoutView | None = None,
    ephemeral: bool = False,
) -> discord.Message | None:
    """Send a followup, swallowing Discord HTTP errors with a log line.

    Pass either `content` (plain text) or `view` (a Components V2 LayoutView) —
    a V2 message carries no plain content. Discord can rate-limit or 5xx the
    followup itself; without this guard the handler would raise inside the event
    loop and the user would see nothing at all. Returns the sent message (so the
    caller can attach a thread to it) or ``None`` if the send failed.
    """
    try:
        # wait=True returns the created WebhookMessage so the caller can attach
        # a thread to it (the default overload is typed to return None).
        if view is not None:
            return await interaction.followup.send(view=view, ephemeral=ephemeral, wait=True)
        return await interaction.followup.send(content or "", ephemeral=ephemeral, wait=True)
    except discord.HTTPException as e:
        logger.warning(
            "followup send failed",
            extra={**_ctx_extra(interaction), "detail": str(e)},
        )
        return None


async def _safe_invoke(
    interaction: discord.Interaction,
    coro: Coroutine[Any, Any, _T],
    *,
    label: str,
) -> _T | None:
    """Run an awaitable, map errors to sanitized Discord replies.

    Returns the awaitable's value on success, None on handled error. Error
    replies are ephemeral (visible only to the invoking user) so backend
    health signals don't leak into the public channel.
    """
    ctx = {**_ctx_extra(interaction), "action": label}
    try:
        return await coro
    except CircuitOpenError:
        logger.warning("backend short-circuited (breaker open)", extra=ctx)
        await _safe_followup(interaction, _USER_CIRCUIT_OPEN, ephemeral=True)
    except BackendTimeoutError:
        logger.warning("backend timed out", extra=ctx)
        await _safe_followup(interaction, _USER_TIMEOUT, ephemeral=True)
    except BackendCallError as e:
        logger.error("backend call error", extra={**ctx, "detail": str(e)})
        await _safe_followup(interaction, _USER_FAIL, ephemeral=True)
    except BackendError:
        logger.exception("unexpected backend error", extra=ctx)
        await _safe_followup(interaction, _USER_FAIL, ephemeral=True)
    return None


def register_handlers(
    tree: app_commands.CommandTree,
    backend: IssueBackend,
    guild_id: int | None,
    *,
    rate_limiter: RateLimiter | None = None,
    app_url: str = "",
    member_map: dict[str, str] | None = None,
    thread_config: ThreadConfig | None = None,
    cards_enabled: bool = False,
) -> None:
    """Attach `/task`, `/status`, `/assign` to `tree`.

    If `guild_id` is provided, commands are registered guild-scoped — updates
    propagate instantly. Global registration takes up to an hour. Tests can
    inject a custom `RateLimiter` (e.g. with a fake clock or a generous
    budget) to keep behaviour deterministic.

    When `thread_config.enabled`, a successful `/task` additionally opens a
    Discord thread for the issue and pings the participants inside it (see
    `threads.py`). The default disabled config preserves prior behaviour.
    """
    guild = discord.Object(id=guild_id) if guild_id else None
    limiter = rate_limiter if rate_limiter is not None else RateLimiter()
    _app_url = app_url.rstrip("/")
    _member_map = member_map or {}
    _thread_config = thread_config or ThreadConfig()
    # Reverse map (Multica member UUID -> Discord id) so a thread can ping the
    # assignee when it was given as a known member UUID — no backend call.
    _reverse_member_map = {uuid: did for did, uuid in _member_map.items()}

    async def _enforce_rate_limit(interaction: discord.Interaction) -> bool:
        key = (
            getattr(getattr(interaction, "guild", None), "id", None),
            getattr(getattr(interaction, "user", None), "id", None),
        )
        if limiter.acquire(key):
            return True
        logger.info("rate limit hit", extra=_ctx_extra(interaction))
        try:
            await interaction.response.send_message(
                _USER_RATE_LIMITED, ephemeral=True
            )
        except discord.HTTPException as e:
            logger.warning(
                "rate-limit reply failed to send",
                extra={**_ctx_extra(interaction), "detail": str(e)},
            )
        return False

    @tree.command(
        name="task",
        description="Создать задачу в трекере",
        guild=guild,
    )
    @app_commands.describe(
        title="Краткое описание задачи",
        description="Подробности (опционально)",
        priority="Приоритет",
        assignee="Кого назначить (имя или ID)",
    )
    @app_commands.choices(
        priority=[app_commands.Choice(name=p, value=p) for p in ALLOWED_PRIORITIES],
    )
    async def task_cmd(
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 300],
        description: app_commands.Range[str, 1, 2000] | None = None,
        priority: app_commands.Choice[str] | None = None,
        assignee: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        if not await _enforce_rate_limit(interaction):
            return
        await interaction.response.defer()
        # Attribute the issue to the real requester. An unmapped invoker falls
        # back to the bot's token owner so task creation never fails on a gap.
        invoker_id = getattr(getattr(interaction, "user", None), "id", None)
        on_behalf_of = _member_map.get(str(invoker_id)) if invoker_id is not None else None
        # Only warn when we actually have an invoker that isn't mapped — not when
        # the interaction carried no user at all (the wording would mislead).
        if _member_map and invoker_id is not None and on_behalf_of is None:
            logger.warning(
                "no Multica member mapping for Discord user; "
                "task will be attributed to the token owner",
                extra=_ctx_extra(interaction),
            )
        ref = await _safe_invoke(
            interaction,
            backend.create_issue(
                title=title,
                description=description,
                priority=priority.value if priority else None,
                assignee=assignee,
                on_behalf_of=on_behalf_of,
            ),
            label="create_issue",
        )
        if ref is None:
            return
        safe_title = discord.utils.escape_markdown(getattr(ref, "title", None) or title)
        identifier = ref.identifier
        if _app_url and ref.id:
            issue_url = f"{_app_url}/venchur/issues/{identifier or ref.id}"
            ref_text = f"[{identifier or ref.id}](<{issue_url}>)"
        else:
            ref_text = f"`{identifier or ref.id}`"
        if cards_enabled:
            message = await _safe_followup(
                interaction,
                view=build_task_card(
                    heading=safe_title,
                    ref_text=ref_text,
                    priority=priority.value if priority else None,
                    description=description,
                ),
            )
        else:
            message = await _safe_followup(
                interaction,
                f"✅ Создана задача **{safe_title}** {ref_text}",
            )
        # Venture thread-per-task: best-effort, never fails the command.
        if _thread_config.enabled and message is not None:
            pings = resolve_thread_pings(
                invoker_id=invoker_id,
                assignee=assignee,
                reverse_member_map=_reverse_member_map,
                ping_user_ids=_thread_config.ping_user_ids,
                ping_role_ids=_thread_config.ping_role_ids,
            )
            await announce_task_thread(
                message=message,
                channel=getattr(interaction, "channel", None),
                ref=ref,
                fallback_title=title,
                app_url=_app_url,
                pings=pings,
                thread_config=_thread_config,
                description=description,
                priority=priority.value if priority else None,
            )

    @tree.command(
        name="status",
        description="Обновить статус задачи",
        guild=guild,
    )
    @app_commands.describe(issue_id="ID задачи", status="Новый статус")
    @app_commands.choices(
        status=[app_commands.Choice(name=s, value=s) for s in ALLOWED_STATUSES],
    )
    async def status_cmd(
        interaction: discord.Interaction,
        issue_id: app_commands.Range[str, 1, 200],
        status: app_commands.Choice[str],
    ) -> None:
        if not await _enforce_rate_limit(interaction):
            return
        await interaction.response.defer()
        ref = await _safe_invoke(
            interaction,
            backend.update_status(issue_id, status.value),
            label="update_status",
        )
        if ref is None:
            return
        await _safe_followup(
            interaction,
            f"✅ Задача `{ref.id}` переведена в **{discord.utils.escape_markdown(status.value)}**.",
        )

    @tree.command(
        name="assign",
        description="Назначить исполнителя на задачу",
        guild=guild,
    )
    @app_commands.describe(issue_id="ID задачи", to="Кого назначить")
    async def assign_cmd(
        interaction: discord.Interaction,
        issue_id: app_commands.Range[str, 1, 200],
        to: app_commands.Range[str, 1, 200],
    ) -> None:
        if not await _enforce_rate_limit(interaction):
            return
        await interaction.response.defer()
        ref = await _safe_invoke(
            interaction,
            backend.assign_issue(issue_id, to),
            label="assign_issue",
        )
        if ref is None:
            return
        await _safe_followup(
            interaction,
            f"✅ Задача `{ref.id}` назначена на **{discord.utils.escape_markdown(to)}**.",
        )
