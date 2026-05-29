"""Unit tests for Multica review routing decisions."""
from __future__ import annotations

import pytest

from discord_agent_secretary.review_routing import classify_review_candidate

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "issue",
    [
        {"id": "i1", "assignee_type": "agent", "origin_type": "autopilot", "origin_id": "ap-1"},
        # origin_source is no longer consulted — any autopilot origin is automated.
        {"id": "i2", "assignee_type": "agent", "origin_type": "autopilot", "origin_source": "schedule"},
        {"id": "i3", "assignee_type": "agent", "origin_type": "autopilot", "origin_source": "manual"},
    ],
)
def test_agent_autopilot_is_suppressed_and_routed(issue: dict[str, object]) -> None:
    decision = classify_review_candidate(issue)
    assert decision.is_automated_autopilot
    assert not decision.notify_discord
    assert decision.reason == "automated_autopilot"


@pytest.mark.parametrize(
    "issue",
    [
        {"id": "ordinary-agent-task", "assignee_type": "agent"},
        {"id": "agent-quick-create", "assignee_type": "agent", "origin_type": "quick_create"},
        {"id": "agent-no-origin", "assignee_type": "agent", "origin_type": None},
    ],
)
def test_non_autopilot_agent_task_remains_notifiable(issue: dict[str, object]) -> None:
    decision = classify_review_candidate(issue)
    assert not decision.is_automated_autopilot
    assert decision.notify_discord
    assert decision.reason == "operator_relevant"


def test_non_agent_assignee_is_not_discord_notifiable() -> None:
    decision = classify_review_candidate(
        {"id": "member-owned-review", "assignee_type": "member", "origin_type": "autopilot"}
    )
    assert not decision.is_automated_autopilot
    assert not decision.notify_discord
    assert decision.reason == "not_agent_assigned"
