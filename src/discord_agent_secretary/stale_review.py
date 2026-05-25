"""Decision helpers for stale Multica issues in review."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from .review_routing import AUTOMATED_AUTOPILOT_SOURCES


@dataclass(frozen=True)
class StaleReviewDecision:
    action: str
    status_target: str | None
    comment: str


@dataclass(frozen=True)
class StaleScanCounts:
    auto_closed: int = 0
    human_escalated: int = 0
    reviewer_escalated: int = 0
    routing_blockers: int = 0


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _age_days(issue: Mapping[str, object]) -> int:
    value = issue.get("age_days")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def classify_stale_in_review(
    issue: Mapping[str, object],
    *,
    routed_state: Mapping[str, Mapping[str, object]],
    threshold_days: int = 7,
) -> StaleReviewDecision:
    if _text(issue.get("status")) != "in_review" or _age_days(issue) <= threshold_days:
        return StaleReviewDecision(action="skip", status_target=None, comment="")

    issue_id = _text(issue.get("id")) or ""
    origin_type = _text(issue.get("origin_type"))
    origin_source = (_text(issue.get("origin_source")) or "").lower()
    creator_type = _text(issue.get("creator_type"))

    if origin_type == "autopilot" and origin_source in AUTOMATED_AUTOPILOT_SOURCES:
        route = routed_state.get(issue_id)
        if route is not None:
            reviewer_ref = _text(route.get("reviewer_ref")) or "configured reviewer"
            return StaleReviewDecision(
                action="reviewer_escalate",
                status_target=None,
                comment=f"Stale automated review - {reviewer_ref}: please approve or request rework.",
            )
        return StaleReviewDecision(
            action="routing_blocker",
            status_target=None,
            comment="Automated review routing blocker: reviewer routing was not recorded.",
        )

    if origin_type == "autopilot":
        return StaleReviewDecision(
            action="human_escalate",
            status_target=None,
            comment="Stale operator-triggered autopilot review - @e.romanov: please review or close.",
        )

    if creator_type == "member":
        return StaleReviewDecision(
            action="human_escalate",
            status_target=None,
            comment="Stale investigation - @e.romanov: please review or close.",
        )

    human_comment_exists = issue.get("human_comment_exists")
    if creator_type == "agent" and human_comment_exists is not True:
        return StaleReviewDecision(
            action="auto_close",
            status_target="done",
            comment="Auto-closed: no human review in 7 days. Autopilot report archived.",
        )

    return StaleReviewDecision(action="skip", status_target=None, comment="")


def format_stale_summary(
    *,
    week_of: date,
    counts: StaleScanCounts,
    oldest_identifier: str | None,
    oldest_age_days: int | None,
) -> str:
    if oldest_identifier and oldest_age_days is not None:
        oldest = f", oldest in_review={oldest_identifier} ({oldest_age_days} days)"
    else:
        oldest = ""
    if (
        counts.auto_closed
        or counts.human_escalated
        or counts.reviewer_escalated
        or counts.routing_blockers
    ):
        return (
            f"Week of {week_of.isoformat()}: auto-closed={counts.auto_closed}, "
            f"human-escalated={counts.human_escalated}, "
            f"reviewer-escalated={counts.reviewer_escalated}, "
            f"routing-blockers={counts.routing_blockers}{oldest}"
        )
    return "All in_review issues within threshold"
