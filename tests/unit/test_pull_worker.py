"""Unit tests for pull-model review notification filtering."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from discord_agent_secretary.pull_worker import ReviewPollWorker, _split_fresh_reviews

pytestmark = pytest.mark.unit


class FakeReviewRouter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.routed_issue_ids: list[str] = []

    async def route_issue(self, issue: dict[str, object]):
        issue_id = str(issue["id"])
        self.routed_issue_ids.append(issue_id)
        if self.fail:
            raise RuntimeError("route failed")
        return SimpleNamespace(outcome="routed", reviewer_ref="checker-agent")


def automated_issue(issue_id: str = "auto-1") -> dict[str, object]:
    return {
        "id": issue_id,
        "identifier": "VEN-1",
        "assignee_type": "agent",
        "origin_type": "autopilot",
        "origin_id": "autopilot-1",
        "origin_source": "schedule",
    }


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

    # All agent-assigned autopilot issues are suppressed+routed regardless of
    # source; the member-assigned one is neither notified nor routed.
    assert [issue["id"] for issue in notifiable] == []
    assert [issue["id"] for issue in suppressed] == ["auto-1", "manual-1"]


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


@pytest.mark.asyncio
async def test_first_pass_routes_existing_automated_reviews_without_discord(
    tmp_path,
    monkeypatch,
) -> None:
    router = FakeReviewRouter()
    worker = ReviewPollWorker(
        cli_path="multica",
        channel_id=1,
        seen_path=tmp_path / "seen.json",
        poll_interval=1,
        app_url="",
        review_router=router,  # type: ignore[arg-type]
    )
    worker._list_in_review = lambda: _async_value([automated_issue()])  # type: ignore[method-assign]

    async def cancel_after_iteration(_: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("discord_agent_secretary.pull_worker.asyncio.sleep", cancel_after_iteration)

    with pytest.raises(asyncio.CancelledError):
        await worker.run(client=object())  # type: ignore[arg-type]

    assert router.routed_issue_ids == ["auto-1"]
    assert json.loads((tmp_path / "seen.json").read_text(encoding="utf-8")) == {
        "seen_ids": ["auto-1"],
    }


@pytest.mark.asyncio
async def test_failed_routing_remains_retryable(tmp_path, monkeypatch) -> None:
    router = FakeReviewRouter(fail=True)
    worker = ReviewPollWorker(
        cli_path="multica",
        channel_id=1,
        seen_path=tmp_path / "seen.json",
        poll_interval=1,
        app_url="",
        review_router=router,  # type: ignore[arg-type]
    )
    polls = 0

    async def list_in_review() -> list[dict[str, object]]:
        nonlocal polls
        polls += 1
        if polls == 1:
            return []
        return [automated_issue()]

    worker._list_in_review = list_in_review  # type: ignore[method-assign]
    sleeps = 0

    async def stop_after_retries(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("discord_agent_secretary.pull_worker.asyncio.sleep", stop_after_retries)

    with pytest.raises(asyncio.CancelledError):
        await worker.run(client=object())  # type: ignore[arg-type]

    assert router.routed_issue_ids == ["auto-1", "auto-1"]


async def _async_value(value: object) -> object:
    return value
