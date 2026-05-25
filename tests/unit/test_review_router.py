"""Unit tests for automated Multica review routing and verdicts."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from discord_agent_secretary.review_router import AutomatedReviewRouter

pytestmark = pytest.mark.unit


@dataclass
class FakeReviewBackend:
    subscribers: list[tuple[str, str]] = field(default_factory=list)
    comments: list[tuple[str, str]] = field(default_factory=list)
    assignments: list[tuple[str, str]] = field(default_factory=list)
    statuses: list[tuple[str, str]] = field(default_factory=list)

    async def add_subscriber(self, issue_id: str, reviewer_ref: str) -> None:
        self.subscribers.append((issue_id, reviewer_ref))

    async def add_comment(self, issue_id: str, content: str) -> None:
        self.comments.append((issue_id, content))

    async def assign_issue(self, issue_id: str, assignee_ref: str) -> None:
        self.assignments.append((issue_id, assignee_ref))

    async def update_status(self, issue_id: str, status: str) -> None:
        self.statuses.append((issue_id, status))


def automated_issue() -> dict[str, object]:
    return {
        "id": "issue-1",
        "identifier": "VEN-1",
        "title": "Scheduled task",
        "assignee_type": "agent",
        "assignee_id": "producer-agent",
        "origin_type": "autopilot",
        "origin_id": "autopilot-1",
        "origin_source": "schedule",
        "status": "in_review",
    }


@pytest.mark.asyncio
async def test_missing_reviewer_config_records_blocker_without_mutation(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = FakeReviewBackend()
    router = AutomatedReviewRouter(
        reviewer_refs=[],
        routing_mode="assign",
        rework_status="todo",
        dry_run=False,
        state_path=tmp_path / "routing.json",
        backend=backend,
    )

    result = await router.route_issue(automated_issue())

    assert result.outcome == "blocked_missing_reviewer"
    assert backend.subscribers == []
    assert backend.comments == []
    assert backend.assignments == []
    assert backend.statuses == []
    assert "automated review routing blocked" in caplog.text


@pytest.mark.asyncio
async def test_route_issue_is_idempotent_and_preserves_producer(tmp_path) -> None:
    backend = FakeReviewBackend()
    router = AutomatedReviewRouter(
        reviewer_refs=["checker-agent"],
        routing_mode="assign",
        rework_status="todo",
        dry_run=False,
        state_path=tmp_path / "routing.json",
        backend=backend,
    )

    first = await router.route_issue(automated_issue())
    second = await router.route_issue(automated_issue())

    assert first.outcome == "routed"
    assert second.outcome == "already_routed"
    assert backend.subscribers == [("issue-1", "checker-agent")]
    assert len(backend.comments) == 1
    assert '"producer_agent_id": "producer-agent"' in backend.comments[0][1]
    assert '"expected_verdicts": ["approve_to_done", "request_rework_to_todo"]' in backend.comments[0][1]
    assert backend.assignments == [("issue-1", "checker-agent")]
    assert router.load_state()["issues"]["issue-1"]["producer_agent_id"] == "producer-agent"


@pytest.mark.asyncio
async def test_corrupt_routing_state_fails_closed(tmp_path) -> None:
    backend = FakeReviewBackend()
    state_path = tmp_path / "routing.json"
    state_path.write_text("{not-json", encoding="utf-8")
    router = AutomatedReviewRouter(
        reviewer_refs=["checker-agent"],
        routing_mode="assign",
        rework_status="todo",
        dry_run=False,
        state_path=state_path,
        backend=backend,
    )

    with pytest.raises(json.JSONDecodeError):
        await router.route_issue(automated_issue())

    assert backend.subscribers == []
    assert backend.comments == []
    assert backend.assignments == []


@pytest.mark.asyncio
async def test_approve_verdict_moves_issue_to_done_with_comment(tmp_path) -> None:
    backend = FakeReviewBackend()
    router = AutomatedReviewRouter(
        reviewer_refs=["checker-agent"],
        routing_mode="subscribe",
        rework_status="todo",
        dry_run=False,
        state_path=tmp_path / "routing.json",
        backend=backend,
    )
    await router.route_issue(automated_issue())

    result = await router.approve("issue-1", reviewer_ref="checker-agent", comment="Looks good")

    assert result.outcome == "approved"
    assert backend.statuses == [("issue-1", "done")]
    assert backend.comments[-1] == (
        "issue-1",
        "[automated-review-verdict] reviewer=checker-agent action=approve: Looks good",
    )


@pytest.mark.asyncio
async def test_approve_verdict_rejects_unrouted_issue(tmp_path) -> None:
    backend = FakeReviewBackend()
    router = AutomatedReviewRouter(
        reviewer_refs=["checker-agent"],
        routing_mode="subscribe",
        rework_status="todo",
        dry_run=False,
        state_path=tmp_path / "routing.json",
        backend=backend,
    )

    result = await router.approve("issue-1", reviewer_ref="checker-agent", comment="Looks good")

    assert result.outcome == "verdict_rejected_unrouted"
    assert backend.statuses == []
    assert backend.comments == []


@pytest.mark.asyncio
async def test_approve_verdict_rejects_wrong_reviewer(tmp_path) -> None:
    backend = FakeReviewBackend()
    router = AutomatedReviewRouter(
        reviewer_refs=["checker-agent"],
        routing_mode="subscribe",
        rework_status="todo",
        dry_run=False,
        state_path=tmp_path / "routing.json",
        backend=backend,
    )
    await router.route_issue(automated_issue())

    result = await router.approve("issue-1", reviewer_ref="other-agent", comment="Looks good")

    assert result.outcome == "verdict_rejected_reviewer"
    assert backend.statuses == []
    assert len(backend.comments) == 1


@pytest.mark.asyncio
async def test_rework_verdict_requires_comment_and_leaves_issue_in_review(tmp_path) -> None:
    backend = FakeReviewBackend()
    router = AutomatedReviewRouter(
        reviewer_refs=["checker-agent"],
        routing_mode="assign",
        rework_status="todo",
        dry_run=False,
        state_path=tmp_path / "routing.json",
        backend=backend,
    )
    await router.route_issue(automated_issue())

    result = await router.request_rework("issue-1", reviewer_ref="checker-agent", comment=" ")

    assert result.outcome == "rework_comment_required"
    assert backend.statuses == []
    assert backend.assignments == [("issue-1", "checker-agent")]


@pytest.mark.asyncio
async def test_rework_verdict_moves_to_rework_status_and_reassigns_producer(tmp_path) -> None:
    backend = FakeReviewBackend()
    router = AutomatedReviewRouter(
        reviewer_refs=["checker-agent"],
        routing_mode="assign",
        rework_status="todo",
        dry_run=False,
        state_path=tmp_path / "routing.json",
        backend=backend,
    )
    await router.route_issue(automated_issue())

    result = await router.request_rework(
        "issue-1",
        reviewer_ref="checker-agent",
        comment="Needs the evidence link fixed",
    )

    assert result.outcome == "rework_requested"
    assert backend.statuses == [("issue-1", "todo")]
    assert backend.assignments == [("issue-1", "checker-agent"), ("issue-1", "producer-agent")]
    assert backend.comments[-1] == (
        "issue-1",
        "[automated-review-verdict] reviewer=checker-agent action=rework: Needs the evidence link fixed",
    )
