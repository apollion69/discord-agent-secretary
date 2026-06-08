"""Passive secretary observer — turn channel chatter into tasks (opt-in).

When ``DISCORD_OBSERVER_ENABLED=true`` the bot watches ``DISCORD_WATCH_CHANNELS``
for messages that start with a trigger prefix (default ``/task``, ``!task``,
``задача:``, ``task:``), parses them with the regex-first
:func:`parsers.parse_task` (RU+EN, no LLM), and posts a **human-in-the-loop**
confirmation with ✅/❌ buttons. Only on ✅ is the issue created — a casual
message never silently spawns a ticket. On ✅ the per-task thread is opened too
(via the shared :func:`threads.announce_task_thread`).

This is the only feature that needs the privileged MESSAGE CONTENT intent, which
stays OFF unless the observer is enabled (see ``discord_client.build_client``)
and is documented in ``docs/threat-model.md``.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import discord

from .backends import BackendError, IssueBackend
from .parsers import ParsedTask, parse_task
from .threads import (
    ThreadConfig,
    announce_task_thread,
    resolve_thread_pings,
)

logger = logging.getLogger(__name__)

DEFAULT_TRIGGERS: tuple[str, ...] = ("/task", "!task", "задача:", "task:")

_USER_FAIL = (
    "⚠️ Не удалось создать задачу — трекер не ответил. Попробуй ещё раз позже."
)
_CONFIRM_TIMEOUT = 300.0


def detect_trigger(content: str, triggers: Sequence[str]) -> str | None:
    """Return the message body after a leading trigger prefix, else ``None``.

    Case-insensitive; an empty body (just the trigger) yields ``None``.
    """
    text = (content or "").lstrip()
    low = text.lower()
    for trig in triggers:
        if low.startswith(trig.lower()):
            body = text[len(trig) :].strip()
            return body or None
    return None


def _description_from_parsed(parsed: ParsedTask) -> str | None:
    """NL mode has no separate description; fold a parsed deadline into one."""
    return f"Дедлайн: {parsed.deadline}" if parsed.deadline else None


def _issue_link(identifier: str, issue_id: str, app_url: str) -> str:
    ref = identifier or issue_id
    if app_url and issue_id:
        return f"[{ref}](<{app_url.rstrip('/')}/venchur/issues/{ref}>)"
    return f"`{ref}`"


def build_preview(parsed: ParsedTask) -> str:
    """The text shown above the ✅/❌ buttons before creating the task."""
    bits: list[str] = []
    if parsed.priority:
        bits.append(f"приоритет: {parsed.priority}")
    if parsed.deadline:
        bits.append(f"дедлайн: {parsed.deadline}")
    if parsed.assignee:
        bits.append(f"исполнитель: @{parsed.assignee}")
    elif parsed.assignee_id:
        bits.append(f"исполнитель: <@{parsed.assignee_id}>")
    tail = (" — " + ", ".join(bits)) if bits else ""
    title = discord.utils.escape_markdown(parsed.title)
    return f"📋 Завести задачу «{title}»?{tail}\nНажми ✅, чтобы создать, ❌ — отменить."


@dataclass(frozen=True)
class ObserverContext:
    """Everything a confirmation view needs to create + announce a task."""

    backend: IssueBackend
    parsed: ParsedTask
    author_id: int | None
    on_behalf_of: str | None
    app_url: str
    thread_config: ThreadConfig
    reverse_member_map: dict[str, str] = field(default_factory=dict)
    extra_ping_user_ids: tuple[int, ...] = ()
    thread_map: Any = None


async def _safe_edit(
    interaction: discord.Interaction, *, content: str, view: discord.ui.View | None
) -> None:
    try:
        await interaction.response.edit_message(content=content, view=view)
    except discord.HTTPException as exc:
        logger.warning("observer: edit_message failed", extra={"detail": str(exc)})


class _CallbackButton(discord.ui.Button[Any]):
    """A Button whose click delegates to an injected coroutine.

    Overriding `callback` in a subclass (rather than assigning to the instance
    attribute) keeps the type checker happy across mypy versions.
    """

    def __init__(
        self,
        *,
        label: str,
        style: discord.ButtonStyle,
        handler: Callable[[discord.Interaction], Awaitable[None]],
    ) -> None:
        super().__init__(label=label, style=style)
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._handler(interaction)


class TaskConfirmView(discord.ui.View):
    """Two-button confirmation: ✅ creates the task (+ thread), ❌ cancels.

    Only the message author may press a button (`interaction_check`). The view
    self-disables and stops after either action or on timeout.
    """

    def __init__(self, ctx: ObserverContext, *, timeout: float = _CONFIRM_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self._confirm = _CallbackButton(
            label="✅ Создать", style=discord.ButtonStyle.success, handler=self._on_confirm
        )
        self._cancel = _CallbackButton(
            label="❌ Отмена", style=discord.ButtonStyle.secondary, handler=self._on_cancel
        )
        self.add_item(self._confirm)
        self.add_item(self._cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.ctx.author_id is None:
            return True
        uid = getattr(getattr(interaction, "user", None), "id", None)
        return uid == self.ctx.author_id

    def _disable(self) -> None:
        self._confirm.disabled = True
        self._cancel.disabled = True

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self._disable()
        self.stop()
        await _safe_edit(interaction, content="❌ Отменено.", view=self)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        self._disable()
        self.stop()
        ctx = self.ctx
        try:
            ref = await ctx.backend.create_issue(
                title=ctx.parsed.title,
                description=_description_from_parsed(ctx.parsed),
                priority=ctx.parsed.priority,
                assignee=ctx.parsed.assignee,
                on_behalf_of=ctx.on_behalf_of,
            )
        except BackendError as exc:
            logger.warning("observer: task create failed", extra={"detail": str(exc)})
            await _safe_edit(interaction, content=_USER_FAIL, view=self)
            return
        link = _issue_link(ref.identifier or "", ref.id, ctx.app_url)
        safe_title = discord.utils.escape_markdown(ref.title or ctx.parsed.title)
        await _safe_edit(
            interaction, content=f"✅ Создана задача **{safe_title}** {link}", view=self
        )
        pings = resolve_thread_pings(
            invoker_id=ctx.author_id,
            assignee=ctx.parsed.assignee,
            reverse_member_map=ctx.reverse_member_map,
            ping_user_ids=ctx.thread_config.ping_user_ids + ctx.extra_ping_user_ids,
            ping_role_ids=ctx.thread_config.ping_role_ids,
        )
        await announce_task_thread(
            message=getattr(interaction, "message", None),
            channel=getattr(interaction, "channel", None),
            ref=ref,
            fallback_title=ctx.parsed.title,
            app_url=ctx.app_url,
            pings=pings,
            thread_config=ctx.thread_config,
            description=_description_from_parsed(ctx.parsed),
            priority=ctx.parsed.priority,
            thread_map=ctx.thread_map,
        )


async def handle_observed_message(
    message: Any,
    *,
    client_user_id: int | None,
    backend: IssueBackend,
    watch_channels: set[int],
    triggers: Sequence[str],
    app_url: str,
    member_map: dict[str, str],
    reverse_member_map: dict[str, str],
    thread_config: ThreadConfig,
    thread_map: Any = None,
) -> TaskConfirmView | None:
    """Core observer logic (decoupled from the gateway for unit testing).

    Returns the posted confirmation view, or ``None`` when the message is
    ignored (bot author, off-channel, no trigger, empty body).
    """
    author = getattr(message, "author", None)
    author_id = getattr(author, "id", None)
    if getattr(author, "bot", False) or (author_id is not None and author_id == client_user_id):
        return None
    channel_id = getattr(getattr(message, "channel", None), "id", None)
    if channel_id not in watch_channels:
        return None
    body = detect_trigger(getattr(message, "content", "") or "", triggers)
    if body is None:
        return None
    parsed = parse_task(body)
    if not parsed.title:
        return None

    on_behalf_of = member_map.get(str(author_id)) if author_id is not None else None
    extra = (parsed.assignee_id,) if parsed.assignee_id is not None else ()
    ctx = ObserverContext(
        backend=backend,
        parsed=parsed,
        author_id=author_id,
        on_behalf_of=on_behalf_of,
        app_url=app_url,
        thread_config=thread_config,
        reverse_member_map=reverse_member_map,
        extra_ping_user_ids=extra,
        thread_map=thread_map,
    )
    view = TaskConfirmView(ctx)
    try:
        await message.reply(
            build_preview(parsed),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
            mention_author=False,
        )
    except discord.HTTPException as exc:
        logger.warning("observer: preview reply failed", extra={"detail": str(exc)})
        return None
    return view


def make_observer_handler(
    client: discord.Client,
    *,
    backend: IssueBackend,
    watch_channels: Sequence[int],
    triggers: Sequence[str],
    app_url: str,
    member_map: dict[str, str],
    thread_config: ThreadConfig,
    thread_map: Any = None,
) -> Callable[[discord.Message], Awaitable[None]]:
    """Build the observer's per-message coroutine (name-agnostic).

    Returned so a single shared `on_message` dispatcher can combine the observer
    with other message handlers (e.g. thread-reply sync) — discord.py allows only
    one `on_message`, so they must not each register their own.
    """
    watch = {int(c) for c in watch_channels}
    reverse = {uuid: did for did, uuid in (member_map or {}).items()}
    _triggers = tuple(triggers) or DEFAULT_TRIGGERS

    async def handler(message: discord.Message) -> None:
        user = client.user
        await handle_observed_message(
            message,
            client_user_id=user.id if user else None,
            backend=backend,
            watch_channels=watch,
            triggers=_triggers,
            app_url=app_url,
            member_map=member_map or {},
            reverse_member_map=reverse,
            thread_config=thread_config,
            thread_map=thread_map,
        )

    return handler


def register_message_observer(
    client: discord.Client,
    *,
    backend: IssueBackend,
    watch_channels: Sequence[int],
    triggers: Sequence[str],
    app_url: str,
    member_map: dict[str, str],
    thread_config: ThreadConfig,
    thread_map: Any = None,
) -> None:
    """Wire the observer as the sole on_message handler on `client`.

    Convenience for an observer-only deployment. When other message handlers are
    needed too, use `make_observer_handler` + `message_router`.
    """
    handler = make_observer_handler(
        client,
        backend=backend,
        watch_channels=watch_channels,
        triggers=triggers,
        app_url=app_url,
        member_map=member_map,
        thread_config=thread_config,
        thread_map=thread_map,
    )

    @client.event
    async def on_message(message: discord.Message) -> None:
        await handler(message)
