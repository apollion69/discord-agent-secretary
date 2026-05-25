"""Unit tests for stale automated review decisions."""
from __future__ import annotations

from datetime import date

import pytest

from discord_agent_secretary.stale_review import (
    StaleScanCounts,
    classify_stale_in_review,
    format_stale_summary,
)

pytestmark = pytest.mark.unit


def base_issue(**updates: object) -> dict[str, object]:
    issue: dict[str, object] = {
        "id": "issue-1",
        "identifier": "VEN-1",
        "status": "in_review",
        "creator_type": "agent",
        "origin_type": "autopilot",
        "origin_source": "schedule",
        "age_days": 8,
    }
    issue.update(updates)
    return issue


def test_stale_automated_issue_with_reviewer_is_escalated_not_closed() -> None:
    decision = classify_stale_in_review(
        base_issue(),
        routed_state={"issue-1": {"reviewer_ref": "checker-agent"}},
    )

    assert decision.action == "reviewer_escalate"
    assert decision.status_target is None
    assert "checker-agent" in decision.comment


def test_stale_automated_issue_without_routing_gets_blocker() -> None:
    decision = classify_stale_in_review(base_issue(), routed_state={})

    assert decision.action == "routing_blocker"
    assert decision.status_target is None
    assert "routing blocker" in decision.comment.lower()


def test_stale_human_investigation_is_escalated_without_status_change() -> None:
    decision = classify_stale_in_review(
        base_issue(creator_type="member", origin_type=None, origin_source=None),
        routed_state={},
    )

    assert decision.action == "human_escalate"
    assert decision.status_target is None
    assert decision.comment == "Stale investigation - @e.romanov: please review or close."


def test_stale_legacy_agent_report_can_be_auto_closed() -> None:
    decision = classify_stale_in_review(
        base_issue(origin_type=None, origin_source=None, human_comment_exists=False),
        routed_state={},
    )

    assert decision.action == "auto_close"
    assert decision.status_target == "done"
    assert decision.comment == "Auto-closed: no human review in 7 days. Autopilot report archived."


def test_under_threshold_issue_is_skipped() -> None:
    decision = classify_stale_in_review(base_issue(age_days=6), routed_state={})

    assert decision.action == "skip"
    assert decision.status_target is None


def test_weekly_summary_includes_new_buckets() -> None:
    summary = format_stale_summary(
        week_of=date(2026, 5, 25),
        counts=StaleScanCounts(
            auto_closed=1,
            human_escalated=2,
            reviewer_escalated=3,
            routing_blockers=4,
        ),
        oldest_identifier="VEN-7",
        oldest_age_days=12,
    )

    assert summary == (
        "Week of 2026-05-25: auto-closed=1, human-escalated=2, "
        "reviewer-escalated=3, routing-blockers=4, oldest in_review=VEN-7 (12 days)"
    )
