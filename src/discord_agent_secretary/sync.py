"""Bidirectional thread↔issue comment sync (opt-in DISCORD_THREAD_SYNC_ENABLED).

Two directions, both gated on a persistent :class:`thread_map.ThreadMap`:

- **inbound** (tracker → Discord): a Multica ``comment_created`` webhook is posted
  into the mapped task thread.
- **outbound** (Discord → tracker): a human reply inside a mapped task thread is
  added as a comment on the issue via ``backend.add_comment``.

Loop guard: every outbound comment carries a ``SYNC_MARKER``; the inbound router
skips any comment containing it, so a Discord-originated comment is never echoed
back into the thread it came from. Bot-authored thread messages are likewise
ignored by the outbound handler.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import discord

from .backends import BackendError, IssueBackend
from .webhook import verify_signature

logger = logging.getLogger(__name__)

# Appended to every Discord-originated comment so the inbound router can skip it.
SYNC_MARKER: Final = "[via-discord]"
_INBOUND_CAP = 1500
_NONE_MENTIONS = discord.AllowedMentions.none()


@dataclass(frozen=True)
class CommentEvent:
    issue_id: str
    identifier: str
    author: str | None
    content: str


def is_discord_origin(content: str | None) -> bool:
    """True if a comment was created by this bot's outbound sync (skip on inbound)."""
    return SYNC_MARKER in (content or "")


def parse_comment_event(
    body: bytes, *, signature: str = "", secret: str = ""
) -> CommentEvent | None:
    """Parse a Multica ``comment_created`` webhook into a CommentEvent.

    HMAC-verified when a `secret` is configured (same scheme as review events).
    Returns ``None`` for any non-comment / malformed / unsigned payload.
    """
    if secret:
        if not signature or not verify_signature(body, signature, secret):
            logger.warning("comment webhook: bad/missing signature — dropping")
            return None
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("event") not in ("comment_created", "comment.created"):
        return None
    issue = data.get("issue")
    comment = data.get("comment")
    if not isinstance(issue, dict) or not isinstance(comment, dict):
        return None
    issue_id = issue.get("id")
    content = comment.get("content")
    if not isinstance(issue_id, str) or not issue_id:
        return None
    if not isinstance(content, str) or not content:
        return None
    author = comment.get("author_name") or comment.get("author")
    return CommentEvent(
        issue_id=issue_id,
        identifier=str(issue.get("identifier") or issue_id),
        author=author if isinstance(author, str) else None,
        content=content,
    )


def format_inbound_post(event: CommentEvent) -> str:
    who = discord.utils.escape_markdown(event.author or "трекер")
    text = discord.utils.escape_markdown((event.content or "").strip())
    if len(text) > _INBOUND_CAP:
        text = text[:_INBOUND_CAP].rstrip() + "…"
    return f"💬 **{who}**: {text}"


async def route_comment_to_thread(
    client: discord.Client,
    thread_map: Any,
    event: CommentEvent,
    *,
    log: logging.Logger | None = None,
) -> bool:
    """Post a tracker comment into its mapped thread. Best-effort; never raises.

    Returns True if posted, False if skipped (Discord-origin, no mapping, or the
    channel is gone/unsendable).
    """
    lg = log or logger
    if is_discord_origin(event.content):
        return False
    thread_id = thread_map.thread_for_issue(event.issue_id)
    if not isinstance(thread_id, int):
        return False
    channel = client.get_channel(thread_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(thread_id)
        except discord.HTTPException as exc:
            lg.warning("sync: inbound thread unavailable", extra={"detail": str(exc)})
            return False
    if not isinstance(channel, discord.abc.Messageable):
        return False
    try:
        await channel.send(format_inbound_post(event), allowed_mentions=_NONE_MENTIONS)
    except discord.HTTPException as exc:
        lg.warning("sync: inbound post failed", extra={"detail": str(exc)})
        return False
    return True


async def handle_thread_reply(
    message: Any,
    *,
    client_user_id: int | None,
    backend: IssueBackend,
    thread_map: Any,
    member_map: dict[str, str],
    log: logging.Logger | None = None,
) -> bool:
    """Outbound: a human reply in a mapped thread → ``backend.add_comment``.

    Ignores bot/self messages, unmapped channels, and empty bodies. Attributes
    the comment to the author via `member_map` (act-as-member) when known, and
    reacts ✅ on success. Returns True if a comment was added.
    """
    lg = log or logger
    author = getattr(message, "author", None)
    author_id = getattr(author, "id", None)
    if getattr(author, "bot", False) or (author_id is not None and author_id == client_user_id):
        return False
    channel_id = getattr(getattr(message, "channel", None), "id", None)
    if not isinstance(channel_id, int):
        return False
    issue_id = thread_map.issue_for_thread(channel_id)
    if not issue_id:
        return False
    content = (getattr(message, "content", "") or "").strip()
    if not content:
        return False
    on_behalf_of = member_map.get(str(author_id)) if author_id is not None else None
    body = f"{content}\n\n{SYNC_MARKER}"
    try:
        await backend.add_comment(issue_id, body, on_behalf_of=on_behalf_of)
    except BackendError as exc:
        lg.warning("sync: outbound add_comment failed", extra={"detail": str(exc)})
        return False
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        pass
    return True


def make_thread_reply_handler(
    client: discord.Client,
    *,
    backend: IssueBackend,
    thread_map: Any,
    member_map: dict[str, str],
) -> Callable[[discord.Message], Awaitable[None]]:
    """Build the outbound per-message coroutine for the message dispatcher."""

    async def handler(message: discord.Message) -> None:
        user = client.user
        await handle_thread_reply(
            message,
            client_user_id=user.id if user else None,
            backend=backend,
            thread_map=thread_map,
            member_map=member_map or {},
        )

    return handler
