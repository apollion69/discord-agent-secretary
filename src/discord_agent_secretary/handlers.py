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
    IssueBackend,
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


class RateLimiter:
    """Tiny token-bucket rate limiter, keyed by anything hashable.

    Default budget — 5 commands burst, refill 1 token / 2 s — keeps the
    backend safe from a single member spamming `/task` while leaving normal
    interactive use unaffected. State is in-process and unbounded; for
    multi-replica deployments swap in a shared store.
    """

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

    def acquire(self, key: Hashable, cost: float = 1.0) -> bool:
        now = self._clock()
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
    content: str,
    *,
    ephemeral: bool = False,
) -> None:
    """Send a followup, swallowing Discord HTTP errors with a log line.

    Discord can rate-limit or 5xx the followup itself; without this guard
    the handler would raise inside the event loop and the user would see
    nothing at all.
    """
    try:
        await interaction.followup.send(content, ephemeral=ephemeral)
    except discord.HTTPException as e:
        logger.warning(
            "followup send failed",
            extra={**_ctx_extra(interaction), "detail": str(e)},
        )


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
    ctx = _ctx_extra(interaction)
    try:
        return await coro
    except BackendTimeoutError:
        logger.warning("%s timed out", label, extra=ctx)
        await _safe_followup(interaction, _USER_TIMEOUT, ephemeral=True)
    except BackendCallError as e:
        logger.error(
            "%s backend call error",
            label,
            extra={**ctx, "detail": str(e)},
        )
        await _safe_followup(interaction, _USER_FAIL, ephemeral=True)
    except BackendError:
        logger.exception("%s unexpected backend error", label, extra=ctx)
        await _safe_followup(interaction, _USER_FAIL, ephemeral=True)
    return None


def register_handlers(
    tree: app_commands.CommandTree,
    backend: IssueBackend,
    guild_id: int | None,
    *,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """Attach `/task`, `/status`, `/assign` to `tree`.

    If `guild_id` is provided, commands are registered guild-scoped — updates
    propagate instantly. Global registration takes up to an hour. Tests can
    inject a custom `RateLimiter` (e.g. with a fake clock or a generous
    budget) to keep behaviour deterministic.
    """
    guild = discord.Object(id=guild_id) if guild_id else None
    limiter = rate_limiter if rate_limiter is not None else RateLimiter()

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
        except discord.HTTPException:
            pass
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
        title: str,
        description: str | None = None,
        priority: app_commands.Choice[str] | None = None,
        assignee: str | None = None,
    ) -> None:
        if not await _enforce_rate_limit(interaction):
            return
        await interaction.response.defer()
        ref = await _safe_invoke(
            interaction,
            backend.create_issue(
                title=title,
                description=description,
                priority=priority.value if priority else None,
                assignee=assignee,
            ),
            label="create_issue",
        )
        if ref is None:
            return
        await _safe_followup(
            interaction,
            f"✅ Создана задача **{getattr(ref, 'title', None) or title}** `{ref.id}`",
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
        issue_id: str,
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
            f"✅ Задача `{ref.id}` переведена в **{status.value}**.",
        )

    @tree.command(
        name="assign",
        description="Назначить исполнителя на задачу",
        guild=guild,
    )
    @app_commands.describe(issue_id="ID задачи", to="Кого назначить")
    async def assign_cmd(
        interaction: discord.Interaction,
        issue_id: str,
        to: str,
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
            f"✅ Задача `{ref.id}` назначена на **{to}**.",
        )
