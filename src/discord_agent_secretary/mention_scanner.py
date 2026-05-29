"""Mention notifier.

Multica has no per-member mention webhook, and the inbox is per-token-owner
(the bot holds one token). So this worker scans comments on active-status
issues for member @mentions (`[@name](mention://member/<member-id>)`) and pings
the corresponding Discord user in the channel.

Coverage is bounded to active statuses (mentions almost always land on active
work); per-issue `updated_at` gates which issues get re-scanned, and a seen-set
dedups individual comments. First pass seeds silently (no historical flood).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import discord

from .approval_buttons import (
    build_approval_view,
    format_approval_request,
    parse_approval_type,
    strip_marker,
)

logger = logging.getLogger(__name__)

# Matches a member mention link and captures the member id.
_MEMBER_MENTION_RE = re.compile(r"\[@?[^\]]*\]\(mention://member/([0-9a-fA-F-]+)\)")
# Renders any mention link as readable "@name" for the snippet.
_ANY_MENTION_RE = re.compile(r"\[(@?[^\]]*)\]\(mention://[^)]+\)")
_SNIPPET_CAP = 140

# Only the user we explicitly ping may be mentioned — comment text (agent- or
# human-authored) can't make the bot ping @everyone/@here or a role.
_ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)


def extract_member_mentions(content: str) -> set[str]:
    return set(_MEMBER_MENTION_RE.findall(content or ""))


def build_member_discord_map(
    members: list[dict[str, Any]], discord_member_map: dict[str, str]
) -> dict[str, str]:
    """member.id -> discord_id, joined via user_id (DISCORD_MEMBER_MAP is discord_id -> user_id)."""
    user_to_discord = {uid: did for did, uid in discord_member_map.items()}
    out: dict[str, str] = {}
    for m in members:
        mid, uid = m.get("id"), m.get("user_id")
        if isinstance(mid, str) and uid in user_to_discord:
            out[mid] = user_to_discord[uid]
    return out


def readable_snippet(content: str) -> str:
    text = _ANY_MENTION_RE.sub(r"\1", content or "").strip().replace("\n", " ")
    # Neutralize markdown so comment text can't inject formatting into Discord.
    text = discord.utils.escape_markdown(text)
    return text[:_SNIPPET_CAP] + ("…" if len(text) > _SNIPPET_CAP else "")


def format_mention_ping(
    discord_id: str, identifier: str, issue_id: str, app_url: str, snippet: str
) -> str:
    ref = (
        f"[{identifier}](<{app_url.rstrip('/')}/venchur/issues/{issue_id}>)"
        if app_url and issue_id
        else identifier
    )
    tail = f": «{snippet}»" if snippet else ""
    return f"\U0001f4ac <@{discord_id}>, тебя упомянули в {ref}{tail}"


# seen-comments is an insertion-ordered set (dict keys) so the bound below
# evicts the OLDEST ids — a plain set + sorted() would evict by arbitrary UUID
# order and could drop recent ids, re-pinging their comments.
def _load_state(path: Path) -> tuple[dict[str, None], dict[str, str]]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return dict.fromkeys(d.get("seen_comments", [])), dict(d.get("issue_seen", {}))
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return {}, {}


def _save_state(path: Path, seen: dict[str, None], issue_seen: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # Cap the seen-set (keep the most recent 5000) so the file can't grow unbounded.
    seen_list = list(seen)[-5000:]
    tmp.write_text(json.dumps({"seen_comments": seen_list, "issue_seen": issue_seen}), encoding="utf-8")
    tmp.replace(path)


def _strip_preamble(raw: str) -> str:
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("{", "[")):
            return "\n".join(lines[i:])
    return raw


class MentionScanWorker:
    def __init__(
        self,
        *,
        cli_path: str,
        channel_id: int,
        app_url: str,
        statuses: list[str],
        discord_member_map: dict[str, str],
        state_path: Path,
        poll_interval: float,
        cli_timeout: float = 30.0,
    ) -> None:
        self._cli_path = cli_path
        self._channel_id = channel_id
        self._app_url = app_url
        self._statuses = statuses
        self._discord_member_map = discord_member_map
        self._state_path = state_path
        self._poll_interval = poll_interval
        self._cli_timeout = cli_timeout

    async def _cli_json(self, *args: str) -> Any:
        proc = await asyncio.create_subprocess_exec(
            self._cli_path, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.gather(proc.stdout.read(), proc.stderr.read()), timeout=self._cli_timeout
            )
        except TimeoutError:
            proc.kill()
            raise
        if proc.returncode is None:
            await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"multica exit {proc.returncode}: {stderr.decode('utf-8','replace')[:200]}")
        text = _strip_preamble(stdout.decode("utf-8", "replace")).strip()
        return json.loads(text) if text else None

    async def _member_discord_map(self) -> dict[str, str]:
        data = await self._cli_json("workspace", "member", "list", "--output", "json")
        members = data.get("members") if isinstance(data, dict) else data
        return build_member_discord_map([m for m in (members or []) if isinstance(m, dict)], self._discord_member_map)

    async def _active_issues(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for status in self._statuses:
            data = await self._cli_json("issue", "list", "--status", status, "--limit", "200", "--output", "json")
            arr = data.get("issues") if isinstance(data, dict) else data
            out.extend(i for i in (arr or []) if isinstance(i, dict) and i.get("id"))
        return out

    async def _send(self, client: discord.Client, message: str, view: discord.ui.View | None = None) -> None:
        channel = client.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(self._channel_id)
            except discord.HTTPException as exc:
                logger.warning("mention channel unavailable", extra={"detail": str(exc)})
                return
        if isinstance(channel, discord.abc.Messageable):
            if view is not None:
                await channel.send(message, view=view, allowed_mentions=_ALLOWED_MENTIONS)
            else:
                await channel.send(message, allowed_mentions=_ALLOWED_MENTIONS)

    async def run(self, client: discord.Client) -> None:
        seen, issue_seen = _load_state(self._state_path)
        first_pass = not seen and not issue_seen
        logger.info("mention_scan_worker started", extra={"seen": len(seen), "first_pass": first_pass})
        while True:
            try:
                member_map = await self._member_discord_map()
                issues = await self._active_issues()
                changed = False
                for issue in issues:
                    iid = str(issue["id"])
                    upd = str(issue.get("updated_at") or "")
                    if issue_seen.get(iid) == upd:
                        continue  # no new activity since last scan
                    issue_seen[iid] = upd
                    changed = True
                    data = await self._cli_json("issue", "comment", "list", iid, "--output", "json")
                    comments = data.get("comments") if isinstance(data, dict) else data
                    for c in (comments or []):
                        cid = str(c.get("id") or "")
                        if not cid or cid in seen:
                            continue
                        seen[cid] = None
                        if first_pass:
                            continue  # seed silently
                        content = c.get("content") or ""
                        approval_type = parse_approval_type(content)
                        identifier = str(issue.get("identifier") or iid)
                        for member_id in extract_member_mentions(content):
                            did = member_map.get(member_id)
                            if not did:
                                continue
                            if approval_type:
                                msg = format_approval_request(
                                    approval_type, did, identifier, readable_snippet(strip_marker(content))
                                )
                                view = build_approval_view(approval_type, iid, self._app_url)
                            else:
                                msg = format_mention_ping(
                                    did, identifier, iid, self._app_url, readable_snippet(content)
                                )
                                view = None
                            await self._send(client, msg, view=view)
                            logger.info(
                                "mention_scan_worker: notified",
                                extra={"issue": identifier, "member_id": member_id, "approval": approval_type},
                            )
                if first_pass or changed:
                    _save_state(self._state_path, seen, issue_seen)
                    first_pass = False
            except asyncio.CancelledError:
                logger.info("mention_scan_worker: cancelled")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("mention_scan_worker: cycle failed", extra={"detail": str(exc)})
            await asyncio.sleep(self._poll_interval)
