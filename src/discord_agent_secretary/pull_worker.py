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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import discord

from ._cli import run_cli_json
from ._worker import backoff_seconds, log_cycle_failure
from .review_router import AutomatedReviewRouter
from .review_routing import classify_review_candidate

logger = logging.getLogger(__name__)

_TERMINAL_ROUTING_OUTCOMES = frozenset(
    {
        "routed",
        "already_routed",
        "dry_run",
        "blocked_missing_reviewer",
        "routing_off",
        "routing_unconfigured",
    }
)


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


def _format_message(issue: dict[str, Any], app_url: str) -> str:
    identifier = str(issue.get("identifier") or issue.get("id", "?"))
    issue_id = str(issue.get("id", ""))
    title = str(issue.get("title") or identifier)
    bell = "\U0001f514"
    if app_url and issue_id:
        url = f"{app_url.rstrip('/')}/venchur/issues/{issue_id}"
        return f"{bell} [{identifier}](<{url}>) — «{title}» переведена агентом в review"
    return f"{bell} {identifier} — «{title}» переведена агентом в review"


def _split_fresh_reviews(
    issues: list[dict[str, Any]], seen: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    notifiable: list[dict[str, Any]] = []
    suppressed_automated: list[dict[str, Any]] = []
    for issue in issues:
        raw_issue_id = issue.get("id")
        if not isinstance(raw_issue_id, str) or raw_issue_id in seen:
            continue

        decision = classify_review_candidate(issue)
        if decision.notify_discord:
            notifiable.append(issue)
        elif decision.is_automated_autopilot:
            suppressed_automated.append(issue)
    return notifiable, suppressed_automated


def _issue_id(issue: dict[str, Any]) -> str:
    return str(issue["id"])


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
        review_router: AutomatedReviewRouter | None = None,
        cli_timeout: float = 30.0,
        failure_alert_threshold: int = 3,
    ) -> None:
        self._cli_path = cli_path
        self._channel_id = channel_id
        self._seen_path = seen_path
        self._poll_interval = poll_interval
        self._app_url = app_url
        self._review_router = review_router
        self._cli_timeout = cli_timeout
        self._failure_alert_threshold = failure_alert_threshold
        self._last_poll_ok: str | None = None

    @property
    def last_poll_ok(self) -> str | None:
        """ISO-8601 UTC timestamp of the most recent successful poll, or None."""
        return self._last_poll_ok

    async def _list_in_review(self) -> list[dict[str, Any]]:
        data = await run_cli_json(
            self._cli_path, "issue", "list", "--status", "in_review",
            "--output", "json", cli_timeout=self._cli_timeout,
        )
        if isinstance(data, dict):
            return [i for i in data.get("issues", []) if isinstance(i, dict)]
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict)]
        return []

    async def _route_suppressed_automated(self, issue: dict[str, Any]) -> bool:
        issue_id = _issue_id(issue)
        routing_outcome = "routing_unconfigured"
        reviewer_ref = None
        if self._review_router is not None:
            try:
                result = await self._review_router.route_issue(issue)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "review_poll_worker: suppressed automated review routing failed",
                    extra={
                        "identifier": issue.get("identifier"),
                        "issue_id": issue_id,
                        "origin_type": issue.get("origin_type"),
                        "origin_id": issue.get("origin_id"),
                        "origin_source": issue.get("origin_source"),
                        "routing_outcome": "routing_failed",
                        "detail": str(exc),
                    },
                )
                return False
            routing_outcome = result.outcome
            reviewer_ref = result.reviewer_ref

        logger.info(
            "review_poll_worker: suppressed automated review",
            extra={
                "identifier": issue.get("identifier"),
                "issue_id": issue_id,
                "origin_type": issue.get("origin_type"),
                "origin_id": issue.get("origin_id"),
                "origin_source": issue.get("origin_source"),
                "routing_outcome": routing_outcome,
                "reviewer_ref": reviewer_ref,
            },
        )
        return routing_outcome in _TERMINAL_ROUTING_OUTCOMES

    async def _reconcile_automated(self, issues: list[dict[str, Any]]) -> None:
        if self._review_router is None:
            return
        reconcile = getattr(self._review_router, "reconcile_issue", None)
        if reconcile is None:
            return
        for issue in issues:
            if not classify_review_candidate(issue).is_automated_autopilot:
                continue
            result = await reconcile(issue)
            if result.outcome not in {"not_routed", "awaiting_verdict", "already_reconciled"}:
                logger.info(
                    "review_poll_worker: automated verdict reconciliation",
                    extra={"issue_id": _issue_id(issue), "outcome": result.outcome},
                )

    async def run(self, client: discord.Client) -> None:
        """Run the poll loop indefinitely. Call from an asyncio task."""
        first_pass = True
        seen = _load_seen(self._seen_path)
        logger.info(
            "review_poll_worker started",
            extra={"seen_count": len(seen), "interval": self._poll_interval},
        )
        consecutive_failures = 0
        while True:
            try:
                issues = await self._list_in_review()
                await self._reconcile_automated(issues)
                current_ids = {i["id"] for i in issues if "id" in i}

                if first_pass:
                    # Seed silently: don't flood with existing in_review issues.
                    _notifiable, suppressed_automated = _split_fresh_reviews(issues, set())
                    suppressed_ids = {_issue_id(i) for i in suppressed_automated}
                    seen = {str(i) for i in current_ids if str(i) not in suppressed_ids}
                    routed_count = 0
                    for issue in suppressed_automated:
                        issue_id = _issue_id(issue)
                        if await self._route_suppressed_automated(issue):
                            seen.add(issue_id)
                            routed_count += 1
                    _save_seen(self._seen_path, seen)
                    first_pass = False
                    logger.info(
                        "review_poll_worker: init pass complete",
                        extra={"seeded": len(seen), "routed_automated": routed_count},
                    )
                else:
                    notifiable, suppressed_automated = _split_fresh_reviews(issues, seen)
                    seen_changed = False
                    for issue in suppressed_automated:
                        issue_id = _issue_id(issue)
                        if await self._route_suppressed_automated(issue):
                            seen.add(issue_id)
                            seen_changed = True

                    for issue in notifiable:
                        issue_id = str(issue["id"])
                        msg = _format_message(issue, self._app_url)
                        await _send_to_channel(client, self._channel_id, msg)
                        seen.add(issue_id)
                        seen_changed = True
                        logger.info(
                            "review_poll_worker: notified",
                            extra={
                                "identifier": issue.get("identifier"),
                                "issue_id": issue_id,
                            },
                        )
                    if seen_changed:
                        _save_seen(self._seen_path, seen)

                self._last_poll_ok = datetime.now(UTC).isoformat()
                consecutive_failures = 0
                sleep_s = self._poll_interval

            except asyncio.CancelledError:
                logger.info("review_poll_worker: cancelled")
                return
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                log_cycle_failure(
                    logger, "review_poll_worker", exc, consecutive_failures, self._failure_alert_threshold
                )
                sleep_s = backoff_seconds(self._poll_interval, consecutive_failures)

            await asyncio.sleep(sleep_s)
