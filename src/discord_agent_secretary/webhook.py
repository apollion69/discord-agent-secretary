"""Multica webhook payload parser and Discord message formatter."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewEvent:
    issue_id: str
    identifier: str
    title: str
    assignee: str | None


def _extract_assignee(issue: dict[str, object]) -> str | None:
    assignee_name = issue.get("assignee_name")
    if isinstance(assignee_name, str):
        return assignee_name
    assignee_display = issue.get("assignee_display_name")
    if isinstance(assignee_display, str):
        return assignee_display
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
    )


def format_review_message(event: ReviewEvent) -> str:
    suffix = f" — {event.assignee}" if event.assignee else ""
    return f"✅ Готово к ревью: **{event.title}** `{event.identifier}`{suffix}"
