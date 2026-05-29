"""Unit tests for the automated review verdict CLI helper."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from discord_agent_secretary.review_verdict import execute_review_action

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Result:
    issue_id: str
    outcome: str
    reviewer_ref: str | None = None


class _Router:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def route_issue(self, issue: dict[str, object]) -> _Result:
        self.calls.append(("route", str(issue["id"]), "", ""))
        return _Result(issue_id=str(issue["id"]), outcome="routed", reviewer_ref="checker-agent")

    async def approve(self, issue_id: str, *, reviewer_ref: str, comment: str) -> _Result:
        self.calls.append(("approve", issue_id, reviewer_ref, comment))
        return _Result(issue_id=issue_id, outcome="approved", reviewer_ref=reviewer_ref)

    async def request_rework(self, issue_id: str, *, reviewer_ref: str, comment: str) -> _Result:
        self.calls.append(("rework", issue_id, reviewer_ref, comment))
        return _Result(issue_id=issue_id, outcome="rework_requested", reviewer_ref=reviewer_ref)


async def test_execute_review_action_routes_issue() -> None:
    router = _Router()

    result = await execute_review_action(
        router=router,
        issue={"id": "issue-1"},
        action="route",
        reviewer_ref="checker-agent",
        comment="",
    )

    assert result["outcome"] == "routed"
    assert router.calls == [("route", "issue-1", "", "")]


async def test_execute_review_action_approves_issue() -> None:
    router = _Router()

    result = await execute_review_action(
        router=router,
        issue={"id": "issue-1"},
        action="approve",
        reviewer_ref="checker-agent",
        comment="Looks good",
    )

    assert result["outcome"] == "approved"
    assert router.calls == [("approve", "issue-1", "checker-agent", "Looks good")]


async def test_execute_review_action_requests_rework() -> None:
    router = _Router()

    result = await execute_review_action(
        router=router,
        issue={"id": "issue-1"},
        action="rework",
        reviewer_ref="checker-agent",
        comment="Fix the evidence",
    )

    assert result["outcome"] == "rework_requested"
    assert router.calls == [("rework", "issue-1", "checker-agent", "Fix the evidence")]


async def test_execute_review_action_requires_issue_id() -> None:
    with pytest.raises(RuntimeError, match="issue payload"):
        await execute_review_action(
            router=_Router(),
            issue={"identifier": "VEN-1"},
            action="approve",
            reviewer_ref="checker-agent",
            comment="Looks good",
        )
