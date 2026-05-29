"""Routing decisions for Multica issues moved to review."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ._cli import text_or_none as _text


@dataclass(frozen=True)
class ReviewRoutingDecision:
    notify_discord: bool
    is_automated_autopilot: bool
    reason: str


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

    # `origin_type=autopilot` is the authoritative cron/autopilot marker exposed
    # by the Multica issue list. `origin_source` is not a first-class issue field,
    # so it is intentionally not required — gating on it would never match and the
    # whole feature stays inert (exactly what happened before origin_type was even
    # exposed in the API).
    origin_type = (_text(issue.get("origin_type")) or "").lower()
    if origin_type == "autopilot":
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
