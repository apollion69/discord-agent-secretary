"""Pull-model review notifier.

Polls `multica issue list --status in_review` every POLL_INTERVAL seconds.
First pass silently seeds seen_ids — no notifications sent on startup.
Subsequent passes notify Discord for issues that are new to the set and
have `assignee_type == "agent"` (best available proxy for agent-driven review
in the absence of an activity-log actor field).

Dedup file: configured via SEEN_ISSUES_PATH (default /opt/discord-secretary/seen.json).
The file is written atomically via a .tmp rename to survive crashes mid-write.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

logger = logging.getLogger(__name__)


def _load_seen(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("seen_ids", []))
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return set()


def _save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seen_ids": sorted(seen)}), encoding="utf-8")
    tmp.replace(path)


def _strip_preamble(raw: str) -> str:
    """Drop leading non-JSON lines such as 'Showing 3 issues.'"""
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("{", "[")):
            return "\n".join(lines[i:])
    return raw


def _format_message(issue: dict[str, Any], app_url: str) -> str:
    identifier = str(issue.get("identifier") or issue.get("id", "?"))
    issue_id = str(issue.get("id", ""))
    title = str(issue.get("title") or identifier)
    bell = "\U0001f514"
    if app_url and issue_id:
        url = f"{app_url.rstrip('/')}/venchur/issues/{issue_id}"
        return f"{bell} [{identifier}](<{url}>) — «{title}» переведена агентом в review"
    return f"{bell} {identifier} — «{title}» переведена агентом в review"


async def _send_to_channel(client: discord.Client, channel_id: int, message: str) -> None:
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            logger.warning(
                "review channel unavailable",
                extra={"channel_id": channel_id, "detail": str(exc)},
            )
            return
    if isinstance(channel, discord.abc.Messageable):
        await channel.send(message)


class ReviewPollWorker:
    """Background coroutine that polls Multica for in_review issues."""

    def __init__(
        self,
        *,
        cli_path: str,
        channel_id: int,
        seen_path: Path,
        poll_interval: float,
        app_url: str,
        cli_timeout: float = 30.0,
    ) -> None:
        self._cli_path = cli_path
        self._channel_id = channel_id
        self._seen_path = seen_path
        self._poll_interval = poll_interval
        self._app_url = app_url
        self._cli_timeout = cli_timeout
        self._last_poll_ok: str | None = None

    @property
    def last_poll_ok(self) -> str | None:
        """ISO-8601 UTC timestamp of the most recent successful poll, or None."""
        return self._last_poll_ok

    async def _list_in_review(self) -> list[dict[str, Any]]:
        # argv-form exec: no shell, no injection risk
        proc = await asyncio.create_subprocess_exec(
            self._cli_path,
            "issue",
            "list",
            "--status",
            "in_review",
            "--output",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.gather(proc.stdout.read(), proc.stderr.read()),
                timeout=self._cli_timeout,
            )
        except TimeoutError:
            proc.kill()
            raise
        if proc.returncode is None:
            await proc.wait()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()[:300]
            raise RuntimeError(f"multica exit {proc.returncode}: {err}")
        text = _strip_preamble(stdout.decode("utf-8", errors="replace")).strip()
        if not text:
            return []
        data: Any = json.loads(text)
        if isinstance(data, dict):
            return [i for i in data.get("issues", []) if isinstance(i, dict)]
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict)]
        return []

    async def run(self, client: discord.Client) -> None:
        """Run the poll loop indefinitely. Call from an asyncio task."""
        first_pass = True
        seen = _load_seen(self._seen_path)
        logger.info(
            "review_poll_worker started",
            extra={"seen_count": len(seen), "interval": self._poll_interval},
        )
        while True:
            try:
                issues = await self._list_in_review()
                current_ids = {i["id"] for i in issues if "id" in i}

                if first_pass:
                    # Seed silently: don't flood with existing in_review issues.
                    seen = current_ids
                    _save_seen(self._seen_path, seen)
                    first_pass = False
                    logger.info(
                        "review_poll_worker: init pass complete",
                        extra={"seeded": len(seen)},
                    )
                else:
                    fresh = [
                        i
                        for i in issues
                        if i.get("id") not in seen
                        and i.get("assignee_type") == "agent"
                    ]
                    for issue in fresh:
                        issue_id = str(issue["id"])
                        msg = _format_message(issue, self._app_url)
                        await _send_to_channel(client, self._channel_id, msg)
                        seen.add(issue_id)
                        logger.info(
                            "review_poll_worker: notified",
                            extra={
                                "identifier": issue.get("identifier"),
                                "issue_id": issue_id,
                            },
                        )
                    if fresh:
                        _save_seen(self._seen_path, seen)

                self._last_poll_ok = datetime.now(timezone.utc).isoformat()

            except asyncio.CancelledError:
                logger.info("review_poll_worker: cancelled")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "review_poll_worker: poll failed",
                    extra={"detail": str(exc)},
                )

            await asyncio.sleep(self._poll_interval)
