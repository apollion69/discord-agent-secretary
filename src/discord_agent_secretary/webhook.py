"""Multica webhook payload parser and Discord message formatter."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

from .review_routing import classify_review_candidate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewEvent:
    issue_id: str
    identifier: str
    title: str
    assignee: str | None
    assignee_type: str | None = None
    assignee_id: str | None = None
    origin_type: str | None = None
    origin_id: str | None = None
    origin_source: str | None = None


def _extract_assignee(issue: dict[str, object]) -> str | None:
    assignee_name = issue.get("assignee_name")
    if isinstance(assignee_name, str):
        return assignee_name
    assignee_display = issue.get("assignee_display_name")
    if isinstance(assignee_display, str):
        return assignee_display
    return None


def _extract_text(issue: dict[str, object], key: str) -> str | None:
    value = issue.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_review_event(
    body: bytes, *, signature: str = "", secret: str = ""
) -> ReviewEvent | None:
    if secret and signature:
        if not verify_signature(body, signature, secret):
            logger.warning("multica webhook: signature mismatch — dropping")
            return None
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        logger.warning("multica webhook: invalid JSON — dropping")
        return None
    if not isinstance(data, dict):
        return None
    if data.get("new_status") != "in_review":
        return None
    if data.get("actor_type") != "agent":
        return None
    issue = data.get("issue")
    if not isinstance(issue, dict):
        return None
    issue_id = issue.get("id")
    if not isinstance(issue_id, str) or not issue_id:
        return None
    return ReviewEvent(
        issue_id=issue_id,
        identifier=str(issue.get("identifier") or issue_id),
        title=str(issue.get("title") or issue_id),
        assignee=_extract_assignee(issue),
        assignee_type=_extract_text(issue, "assignee_type"),
        assignee_id=_extract_text(issue, "assignee_id"),
        origin_type=_extract_text(issue, "origin_type"),
        origin_id=_extract_text(issue, "origin_id"),
        origin_source=_extract_text(issue, "origin_source"),
    )


def should_notify_discord_for_review(event: ReviewEvent) -> bool:
    decision = classify_review_candidate(
        {
            "id": event.issue_id,
            "assignee_type": event.assignee_type or "agent",
            "assignee_id": event.assignee_id,
            "origin_type": event.origin_type,
            "origin_id": event.origin_id,
            "origin_source": event.origin_source,
        }
    )
    return decision.notify_discord


def format_review_message(event: ReviewEvent) -> str:
    suffix = f" — {event.assignee}" if event.assignee else ""
    return f"✅ Готово к ревью: **{event.title}** `{event.identifier}`{suffix}"
