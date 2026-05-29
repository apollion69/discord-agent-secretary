"""Unit tests for the pull poller's notification selection."""
from __future__ import annotations

import pytest

from discord_agent_secretary.pull_worker import select_fresh

pytestmark = pytest.mark.unit


def _issue(id_: str, *, assignee="agent", origin=None):
    d = {"id": id_, "assignee_type": assignee}
    if origin is not None:
        d["origin_type"] = origin
    return d


class TestSelectFresh:
    def test_autopilot_issue_is_suppressed(self):
        issues = [_issue("a", origin="autopilot")]
        assert select_fresh(issues, set()) == []

    def test_human_agent_issue_is_notified(self):
        issues = [_issue("a", origin=None)]  # no autopilot origin
        assert [i["id"] for i in select_fresh(issues, set())] == ["a"]

    def test_quick_create_is_notified(self):
        issues = [_issue("a", origin="quick_create")]
        assert [i["id"] for i in select_fresh(issues, set())] == ["a"]

    def test_non_agent_assignee_is_skipped(self):
        issues = [_issue("a", assignee="squad", origin=None)]
        assert select_fresh(issues, set()) == []

    def test_seen_issue_is_skipped(self):
        issues = [_issue("a", origin=None)]
        assert select_fresh(issues, {"a"}) == []

    def test_mixed_batch(self):
        issues = [
            _issue("auto", origin="autopilot"),
            _issue("human", origin=None),
            _issue("seen", origin=None),
            _issue("quick", origin="quick_create"),
        ]
        got = {i["id"] for i in select_fresh(issues, {"seen"})}
        assert got == {"human", "quick"}
