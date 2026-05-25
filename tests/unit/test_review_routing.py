"""Unit tests for Multica review routing decisions."""
from __future__ import annotations

import pytest

from discord_agent_secretary.review_routing import classify_review_candidate

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("source", ["schedule", "webhook", "api"])
def test_automated_autopilot_sources_are_suppressed_from_discord(source: str) -> None:
    decision = classify_review_candidate(
        {
            "id": "issue-1",
            "assignee_type": "agent",
            "origin_type": "autopilot",
            "origin_id": "autopilot-1",
            "origin_source": source,
        }
    )

    assert decision.is_automated_autopilot
    assert not decision.notify_discord
    assert decision.reason == "automated_autopilot"


@pytest.mark.parametrize(
    "issue",
    [
        {
            "id": "manual-autopilot",
            "assignee_type": "agent",
            "origin_type": "autopilot",
            "origin_source": "manual",
        },
        {
            "id": "missing-source",
            "assignee_type": "agent",
            "origin_type": "autopilot",
        },
        {
            "id": "ordinary-agent-task",
            "assignee_type": "agent",
        },
    ],
)
def test_manual_or_unknown_origin_remains_discord_notifiable(
    issue: dict[str, object],
) -> None:
    decision = classify_review_candidate(issue)

    assert not decision.is_automated_autopilot
    assert decision.notify_discord
    assert decision.reason == "operator_relevant"


def test_non_agent_assignee_is_not_discord_notifiable() -> None:
    decision = classify_review_candidate(
        {
            "id": "member-owned-review",
            "assignee_type": "member",
            "origin_type": "autopilot",
            "origin_source": "manual",
        }
    )

    assert not decision.is_automated_autopilot
    assert not decision.notify_discord
    assert decision.reason == "not_agent_assigned"
