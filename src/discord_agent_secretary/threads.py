"""Thread-per-task ("Venture secretary") support.

When ``/task`` creates an issue, the bot opens a dedicated Discord **thread** for
it and pings the relevant people *inside the thread* — keeping the main channel
uncluttered while every task gets its own jump-able discussion space.

The pure helpers (:func:`build_thread_name`, :func:`resolve_thread_pings`,
:func:`build_allowed_mentions`, :func:`build_thread_intro`) are deterministic and
unit-tested without touching Discord. :func:`open_task_thread` is the
best-effort async orchestrator — it never raises into the slash-command handler:
a failure to open a thread must not fail task creation, which has *already*
succeeded.

Permissions: opening a public thread needs ``create_public_threads`` and posting
in it needs ``send_messages_in_threads`` — neither is in the bot's refused-perms
allow-list, so the feature is compatible with the minimal-privilege posture.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import discord

logger = logging.getLogger(__name__)

# Discord's hard limit on a thread name.
THREAD_NAME_MAX: Final[int] = 100
# Discord accepts only these auto-archive durations (minutes).
VALID_AUTO_ARCHIVE: Final[frozenset[int]] = frozenset({60, 1440, 4320, 10080})
# Intro body cap so a long /task description can't post a wall of text.
_INTRO_DESC_CAP: Final[int] = 1500
_ELLIPSIS: Final[str] = "…"


@dataclass(frozen=True)
class ThreadConfig:
    """Immutable bundle of the thread-per-task knobs (see ``config.Settings``)."""

    enabled: bool = False
    private: bool = False
    auto_archive_minutes: int = 4320
    name_max_words: int = 6
    ping_user_ids: tuple[int, ...] = ()
    ping_role_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ThreadPings:
    """Resolved set of ids to @mention inside a task thread."""

    user_ids: tuple[int, ...] = ()
    role_ids: tuple[int, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.user_ids and not self.role_ids


def build_thread_name(
    identifier: str | None,
    title: str | None,
    *,
    max_len: int = THREAD_NAME_MAX,
    max_words: int = 0,
) -> str:
    """Compose ``"<identifier> <short title>"`` within Discord's 100-char limit.

    - Collapses internal whitespace in the title.
    - When ``max_words > 0``, keeps only the first ``max_words`` title words.
    - Hard-caps the whole name at ``max_len``, ellipsizing on truncation.
    - Degrades to the bare title when ``identifier`` is empty, and to a generic
      ``"task"`` label when both are empty.
    """
    ident = (identifier or "").strip()
    words = (title or "").split()
    if max_words and max_words > 0:
        words = words[:max_words]
    short_title = " ".join(words)
    name = " ".join(p for p in (ident, short_title) if p).strip()
    if not name:
        name = "task"
    if len(name) > max_len:
        cut = max(1, max_len - len(_ELLIPSIS))
        name = name[:cut].rstrip() + _ELLIPSIS
    return name


def resolve_thread_pings(
    *,
    invoker_id: int | None,
    assignee: str | None = None,
    reverse_member_map: Mapping[str, str] | None = None,
    ping_user_ids: Iterable[int] = (),
    ping_role_ids: Iterable[int] = (),
) -> ThreadPings:
    """Resolve who to @mention inside the task thread.

    Always the creator. Plus the assignee *iff* it resolves to a Discord user
    through the reverse member map (Multica member UUID → Discord id) — a
    name-based assignee yields no ping (graceful, zero backend coupling). Plus
    the configured standing watchers (user + role ids). De-duplicated, creator
    first, insertion-ordered. ``@everyone`` is never produced.
    """
    users: list[int] = []

    def _add(uid: int | None) -> None:
        if uid is not None and uid not in users:
            users.append(uid)

    _add(invoker_id)

    rmap = reverse_member_map or {}
    if assignee:
        mapped = rmap.get(assignee.strip())
        if mapped is not None:
            try:
                _add(int(mapped))
            except (TypeError, ValueError):
                pass

    for uid in ping_user_ids:
        _add(int(uid))

    roles: list[int] = []
    for rid in ping_role_ids:
        r = int(rid)
        if r not in roles:
            roles.append(r)

    return ThreadPings(user_ids=tuple(users), role_ids=tuple(roles))


def build_allowed_mentions(pings: ThreadPings) -> discord.AllowedMentions:
    """Scope mentions to EXACTLY the resolved ids — never ``@everyone``/``@here``.

    Even if thread content somehow contained a stray mention, Discord notifies
    only the ids enumerated here. An empty category means "ping nobody of that
    kind".
    """
    return discord.AllowedMentions(
        everyone=False,
        users=[discord.Object(id=uid) for uid in pings.user_ids],
        roles=[discord.Object(id=rid) for rid in pings.role_ids],
    )


def _mentions_text(pings: ThreadPings) -> str:
    return " ".join(
        [f"<@{u}>" for u in pings.user_ids] + [f"<@&{r}>" for r in pings.role_ids]
    )


def _issue_link(identifier: str, issue_id: str, app_url: str) -> str:
    ref = identifier or issue_id
    if app_url and issue_id:
        return f"[{ref}](<{app_url.rstrip('/')}/venchur/issues/{ref}>)"
    return f"`{ref}`"


def build_thread_intro(
    *,
    identifier: str,
    issue_id: str,
    title: str | None = None,
    app_url: str = "",
    pings: ThreadPings,
    description: str | None = None,
    priority: str | None = None,
) -> str:
    """Build the rich first message posted *inside* the task thread.

    The main channel keeps only the short confirmation; the detail (link,
    priority, description) and the participant pings live here.
    """
    safe_title = discord.utils.escape_markdown(title or identifier or issue_id)
    link = _issue_link(identifier, issue_id, app_url)
    lines: list[str] = [f"\U0001f9f5 **{safe_title}** — {link}"]
    if priority:
        lines.append(f"Приоритет: **{discord.utils.escape_markdown(priority)}**")
    if description and description.strip():
        desc = discord.utils.escape_markdown(description.strip())
        if len(desc) > _INTRO_DESC_CAP:
            desc = desc[:_INTRO_DESC_CAP].rstrip() + _ELLIPSIS
        lines += ["", desc]
    mentions = _mentions_text(pings)
    if mentions:
        lines += [
            "",
            f"{mentions} — задача на вас. Обсуждаем здесь, основной канал не засоряем.",
        ]
    return "\n".join(lines)


async def open_task_thread(
    *,
    message: Any,
    channel: Any,
    name: str,
    intro: str,
    allowed_mentions: discord.AllowedMentions,
    private: bool = False,
    auto_archive_minutes: int = 4320,
    log: logging.Logger | None = None,
) -> Any | None:
    """Best-effort: open the task thread and post the intro. Never raises.

    Public (default): the thread is attached to the announcement ``message`` so
    the main channel shows the task with its thread hanging off it. Private: a
    standalone private thread is created on the parent ``channel`` (members are
    pulled in by the intro mentions). Any Discord error is logged and swallowed
    — the ``/task`` reply has already gone out, so the user is never left in a
    failed state because a thread could not be opened.
    """
    lg = log or logger
    try:
        if private:
            thread = await channel.create_thread(
                name=name,
                type=discord.ChannelType.private_thread,
                auto_archive_duration=auto_archive_minutes,
                invitable=True,
            )
        else:
            thread = await message.create_thread(
                name=name,
                auto_archive_duration=auto_archive_minutes,
            )
    except (discord.HTTPException, AttributeError, TypeError) as exc:
        lg.warning(
            "task thread creation failed",
            extra={"detail": str(exc), "thread_name": name, "private": private},
        )
        return None
    try:
        await thread.send(intro, allowed_mentions=allowed_mentions)
    except discord.HTTPException as exc:
        lg.warning("task thread intro post failed", extra={"detail": str(exc)})
    return thread
