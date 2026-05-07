"""Slash-command handlers bridging Discord to a pluggable issue backend.

Each handler:
  1. Defers the Discord response (backend calls can take several seconds).
  2. Invokes the injected `IssueBackend` (see `backends/base.py`).
  3. Replies with outcome or a sanitized error — raw backend stderr never
     leaks to the user-visible channel.

Handlers are backend-agnostic: they catch `BackendTimeoutError`, `BackendCallError`,
`BackendError` from the abstract hierarchy. Concrete backends raise their own
subclasses for log richness.
"""
from __future__ import annotations

import logging
from collections.abc import Coroutine
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


async def _safe_invoke(
    interaction: discord.Interaction,
    coro: Coroutine[Any, Any, _T],
    *,
    label: str,
) -> _T | None:
    """Run an awaitable, map errors to sanitized Discord replies.

    Returns the awaitable's value on success, None on handled error.
    """
    try:
        return await coro
    except BackendTimeoutError:
        logger.warning("%s timed out", label)
        await interaction.followup.send(_USER_TIMEOUT)
    except BackendCallError as e:
        logger.error("%s backend call error", label, extra={"detail": str(e)})
        await interaction.followup.send(_USER_FAIL)
    except BackendError:
        logger.exception("%s unexpected backend error", label)
        await interaction.followup.send(_USER_FAIL)
    return None


def register_handlers(
    tree: app_commands.CommandTree,
    backend: IssueBackend,
    guild_id: int | None,
) -> None:
    """Attach `/task`, `/status`, `/assign` to `tree`.

    If `guild_id` is provided, commands are registered guild-scoped — updates
    propagate instantly. Global registration takes up to an hour.
    """
    guild = discord.Object(id=guild_id) if guild_id else None

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
        await interaction.followup.send(
            f"✅ Создана задача **{getattr(ref, 'title', None) or title}** `{ref.id}`"
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
        await interaction.response.defer()
        ref = await _safe_invoke(
            interaction,
            backend.update_status(issue_id, status.value),
            label="update_status",
        )
        if ref is None:
            return
        await interaction.followup.send(
            f"✅ Задача `{ref.id}` переведена в **{status.value}**."
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
        await interaction.response.defer()
        ref = await _safe_invoke(
            interaction,
            backend.assign_issue(issue_id, to),
            label="assign_issue",
        )
        if ref is None:
            return
        await interaction.followup.send(
            f"✅ Задача `{ref.id}` назначена на **{to}**."
        )
