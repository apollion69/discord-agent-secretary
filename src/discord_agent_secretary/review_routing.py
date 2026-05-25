"""Routing decisions for Multica issues moved to review."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

AUTOMATED_AUTOPILOT_SOURCES = frozenset({"schedule", "webhook", "api"})


@dataclass(frozen=True)
class ReviewRoutingDecision:
    notify_discord: bool
    is_automated_autopilot: bool
    reason: str


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def classify_review_candidate(issue: Mapping[str, object]) -> ReviewRoutingDecision:
    """Classify whether an in_review issue should notify corporate Discord.

    Only first-class origin metadata is considered for automation suppression.
    Titles are intentionally ignored so human/operator review tasks cannot be
    hidden by naming collisions.
    """
    assignee_type = _text(issue.get("assignee_type"))
    if assignee_type != "agent":
        return ReviewRoutingDecision(
            notify_discord=False,
            is_automated_autopilot=False,
            reason="not_agent_assigned",
        )

    origin_type = (_text(issue.get("origin_type")) or "").lower()
    origin_source = (_text(issue.get("origin_source")) or "").lower()
    if origin_type == "autopilot" and origin_source in AUTOMATED_AUTOPILOT_SOURCES:
        return ReviewRoutingDecision(
            notify_discord=False,
            is_automated_autopilot=True,
            reason="automated_autopilot",
        )

    return ReviewRoutingDecision(
        notify_discord=True,
        is_automated_autopilot=False,
        reason="operator_relevant",
    )
