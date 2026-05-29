"""Daily autopilot digest.

The pull poller suppresses per-task notifications for autopilot (cron) issues
(`origin_type == "autopilot"`). This worker replaces that noise with one
message per day: a compact summary of autopilot issues awaiting review and
those completed in the last 24h.

Scheduling is timezone-aware (the configured TZ) and fires once at
`digest_hour`. A small state file records the last posted date so a restart
within the same day does not double-post. The loop re-checks at least hourly
so a missed window (downtime) self-corrects on the next tick.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import discord

logger = logging.getLogger(__name__)

_WINDOW = timedelta(hours=24)
_MAX_LIST = 8  # identifiers shown per bucket before "+N ещё"


def _load_last_date(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("last_date")
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return None


def _save_last_date(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_date": value}), encoding="utf-8")
    tmp.replace(path)


def _strip_preamble(raw: str) -> str:
    for i, line in enumerate(raw.splitlines()):
        if line.lstrip().startswith(("{", "[")):
            return "\n".join(raw.splitlines()[i:])
    return raw


def _is_autopilot(issue: dict[str, Any]) -> bool:
    return issue.get("origin_type") == "autopilot"


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _link(issue: dict[str, Any], app_url: str) -> str:
    ident = str(issue.get("identifier") or issue.get("id", "?"))
    issue_id = str(issue.get("id", ""))
    if app_url and issue_id:
        return f"[{ident}](<{app_url.rstrip('/')}/venchur/issues/{issue_id}>)"
    return ident


def _bucket_line(label: str, issues: list[dict[str, Any]], app_url: str) -> str:
    shown = ", ".join(_link(i, app_url) for i in issues[:_MAX_LIST])
    extra = f" (+{len(issues) - _MAX_LIST} ещё)" if len(issues) > _MAX_LIST else ""
    tail = f" — {shown}{extra}" if issues else ""
    return f"• {label}: {len(issues)}{tail}"


def format_digest(
    in_review: list[dict[str, Any]],
    done_recent: list[dict[str, Any]],
    app_url: str,
) -> str | None:
    """Build the digest message, or None if there is nothing to report."""
    if not in_review and not done_recent:
        return None
    return (
        "\U0001f5d2️ **Сводка авто-задач за сутки**\n"
        + _bucket_line("В review", in_review, app_url)
        + "\n"
        + _bucket_line("Выполнено за 24ч", done_recent, app_url)
        + "\n_Уведомления по отдельным авто-задачам отключены._"
    )


class DigestWorker:
    """Background coroutine that posts a daily autopilot digest."""

    def __init__(
        self,
        *,
        cli_path: str,
        channel_id: int,
        app_url: str,
        tz: str,
        digest_hour: int,
        state_path: Path,
        cli_timeout: float = 30.0,
        now_fn: Any = None,
    ) -> None:
        self._cli_path = cli_path
        self._channel_id = channel_id
        self._app_url = app_url
        self._tz = ZoneInfo(tz)
        self._hour = digest_hour
        self._state_path = state_path
        self._cli_timeout = cli_timeout
        # Injectable clock for tests; defaults to real tz-aware now.
        self._now = now_fn or (lambda: datetime.now(self._tz))

    async def _list(self, status: str) -> list[dict[str, Any]]:
        proc = await asyncio.create_subprocess_exec(
            self._cli_path, "issue", "list", "--status", status,
            "--limit", "500", "--output", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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
            raise RuntimeError(f"multica exit {proc.returncode}: {stderr.decode('utf-8', 'replace')[:200]}")
        text = _strip_preamble(stdout.decode("utf-8", "replace")).strip()
        if not text:
            return []
        data: Any = json.loads(text)
        items = data.get("issues", []) if isinstance(data, dict) else data
        return [i for i in items if isinstance(i, dict)]

    async def build(self) -> str | None:
        in_review = [i for i in await self._list("in_review") if _is_autopilot(i)]
        cutoff = datetime.now(UTC) - _WINDOW
        done_recent = [
            i
            for i in await self._list("done")
            if _is_autopilot(i)
            and (ts := _parse_ts(i.get("updated_at"))) is not None
            and ts >= cutoff
        ]
        return format_digest(in_review, done_recent, self._app_url)

    async def _post(self, client: discord.Client, message: str) -> None:
        channel = client.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(self._channel_id)
            except discord.HTTPException as exc:
                logger.warning("digest channel unavailable", extra={"detail": str(exc)})
                return
        if isinstance(channel, discord.abc.Messageable):
            await channel.send(message)

    def _due(self, now: datetime, last: str | None) -> bool:
        return now.hour >= self._hour and last != now.date().isoformat()

    def _sleep_seconds(self, now: datetime) -> float:
        target = now.replace(hour=self._hour, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        # Cap at 1h so a missed window (downtime) self-corrects promptly.
        return min((target - now).total_seconds(), 3600.0)

    async def run(self, client: discord.Client) -> None:
        last = _load_last_date(self._state_path)
        logger.info("digest_worker started", extra={"hour": self._hour, "last_date": last})
        while True:
            try:
                now = self._now()
                if self._due(now, last):
                    message = await self.build()
                    if message is not None:
                        await self._post(client, message)
                        logger.info("digest_worker: posted", extra={"date": now.date().isoformat()})
                    else:
                        logger.info("digest_worker: nothing to report", extra={"date": now.date().isoformat()})
                    last = now.date().isoformat()
                    _save_last_date(self._state_path, last)
                sleep_s = self._sleep_seconds(self._now())
            except asyncio.CancelledError:
                logger.info("digest_worker: cancelled")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("digest_worker: cycle failed", extra={"detail": str(exc)})
                sleep_s = 3600.0
            await asyncio.sleep(sleep_s)
