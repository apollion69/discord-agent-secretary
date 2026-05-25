"""Unit tests for pull-model review notification filtering."""
from __future__ import annotations

import pytest

from discord_agent_secretary.pull_worker import _split_fresh_reviews

pytestmark = pytest.mark.unit


def test_split_fresh_reviews_suppresses_automated_autopilot() -> None:
    notifiable, suppressed = _split_fresh_reviews(
        [
            {
                "id": "auto-1",
                "assignee_type": "agent",
                "origin_type": "autopilot",
                "origin_source": "schedule",
            },
            {
                "id": "manual-1",
                "assignee_type": "agent",
                "origin_type": "autopilot",
                "origin_source": "manual",
            },
            {
                "id": "member-1",
                "assignee_type": "member",
                "origin_type": "autopilot",
                "origin_source": "manual",
            },
        ],
        seen=set(),
    )

    assert [issue["id"] for issue in notifiable] == ["manual-1"]
    assert [issue["id"] for issue in suppressed] == ["auto-1"]


def test_split_fresh_reviews_ignores_seen_ids() -> None:
    notifiable, suppressed = _split_fresh_reviews(
        [
            {
                "id": "auto-1",
                "assignee_type": "agent",
                "origin_type": "autopilot",
                "origin_source": "schedule",
            },
            {
                "id": "ordinary-1",
                "assignee_type": "agent",
            },
        ],
        seen={"auto-1", "ordinary-1"},
    )

    assert notifiable == []
    assert suppressed == []
