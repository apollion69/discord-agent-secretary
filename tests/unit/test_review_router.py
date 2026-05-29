"""Unit tests for automated Multica review routing and verdicts."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from discord_agent_secretary.review_router import AutomatedReviewRouter, CliReviewBackend

pytestmark = pytest.mark.unit


@dataclass
class FakeReviewBackend:
    listed_comments: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    subscribers: list[tuple[str, str]] = field(default_factory=list)
    comments: list[tuple[str, str]] = field(default_factory=list)
    assignments: list[tuple[str, str]] = field(default_factory=list)
    statuses: list[tuple[str, str]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    async def list_comments(self, issue_id: str) -> list[dict[str, object]]:
        return self.listed_comments.get(issue_id, [])

    async def add_subscriber(self, issue_id: str, reviewer_ref: str) -> None:
        self.subscribers.append((issue_id, reviewer_ref))
        self.calls.append("subscriber")

    async def add_comment(self, issue_id: str, content: str) -> None:
        self.comments.append((issue_id, content))
        self.calls.append("comment")

    async def assign_issue(self, issue_id: str, assignee_ref: str) -> None:
        self.assignments.append((issue_id, assignee_ref))
        self.calls.append("assign")

    async def update_status(self, issue_id: str, status: str) -> None:
        self.statuses.append((issue_id, status))
        self.calls.append("status")


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
async def test_assign_mode_reassigns_reviewer_before_routing_comment(tmp_path) -> None:
    """In assign mode the reviewer must be assigned BEFORE the routing comment.

    The routing comment dispatches the issue assignee. If the producer is still
    assigned when the comment lands, the producer (e.g. Codex-worker) wakes into a
    no-op session. Assigning the reviewer first makes the comment dispatch the
    reviewer instead, eliminating the wasteful producer trigger.
    """
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

    assert backend.calls == ["subscriber", "assign", "comment"]
    assert backend.calls.index("assign") < backend.calls.index("comment")


@pytest.mark.asyncio
async def test_subscribe_mode_posts_comment_without_reassigning(tmp_path) -> None:
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

    assert backend.calls == ["subscriber", "comment"]
    assert backend.assignments == []


@pytest.mark.asyncio
async def test_route_issue_hydrates_existing_routing_comment_after_state_loss(tmp_path) -> None:
    comment = {
        "content": (
            '[automated-review-routing] {"issue_id": "issue-1", '
            '"reviewer_ref": "checker-agent", "producer_agent_id": "producer-agent"}'
        )
    }
    backend = FakeReviewBackend(listed_comments={"issue-1": [comment]})
    router = AutomatedReviewRouter(
        reviewer_refs=["checker-agent"],
        routing_mode="subscribe",
        rework_status="todo",
        dry_run=False,
        state_path=tmp_path / "routing.json",
        backend=backend,
    )

    result = await router.route_issue(automated_issue())

    assert result.outcome == "already_routed"
    assert result.reviewer_ref == "checker-agent"
    assert backend.subscribers == []
    assert backend.comments == []
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
async def test_approve_verdict_hydrates_routing_comment_after_state_loss(tmp_path) -> None:
    comment = {
        "content": (
            '[automated-review-routing] {"issue_id": "issue-1", '
            '"reviewer_ref": "checker-agent", "producer_agent_id": "producer-agent"}'
        )
    }
    backend = FakeReviewBackend(listed_comments={"issue-1": [comment]})
    router = AutomatedReviewRouter(
        reviewer_refs=["checker-agent"],
        routing_mode="subscribe",
        rework_status="todo",
        dry_run=False,
        state_path=tmp_path / "routing.json",
        backend=backend,
    )

    result = await router.approve("issue-1", reviewer_ref="checker-agent", comment="Looks good")

    assert result.outcome == "approved"
    assert backend.statuses == [("issue-1", "done")]
    assert router.load_state()["issues"]["issue-1"]["reviewer_ref"] == "checker-agent"


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


@pytest.mark.asyncio
async def test_cli_backend_assigns_uuid_refs_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = CliReviewBackend("multica")
    calls: list[tuple[str, ...]] = []

    async def _fake_run(*args: str, stdin_text: str | None = None) -> tuple[bytes, bytes]:
        calls.append(args)
        return b"{}", b""

    monkeypatch.setattr(backend, "_run", _fake_run)

    await backend.assign_issue("issue-1", "d28180c2-6f1c-4214-9ca1-140bd14f36db")

    assert calls == [
        (
            "issue",
            "assign",
            "issue-1",
            "--to-id",
            "d28180c2-6f1c-4214-9ca1-140bd14f36db",
            "--output",
            "json",
        )
    ]


@pytest.mark.asyncio
async def test_cli_backend_subscribes_named_refs_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = CliReviewBackend("multica")
    calls: list[tuple[str, ...]] = []

    async def _fake_run(*args: str, stdin_text: str | None = None) -> tuple[bytes, bytes]:
        calls.append(args)
        return b"{}", b""

    monkeypatch.setattr(backend, "_run", _fake_run)

    await backend.add_subscriber("issue-1", "Codex-worker")

    assert calls == [
        (
            "issue",
            "subscriber",
            "add",
            "issue-1",
            "--user",
            "Codex-worker",
            "--output",
            "json",
        )
    ]
