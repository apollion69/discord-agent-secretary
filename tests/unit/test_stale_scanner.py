"""Unit tests for the production stale in_review scanner."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from discord_agent_secretary.review_router import ROUTING_COMMENT_PREFIX
from discord_agent_secretary.stale_scanner import CliStaleReviewBackend, StaleReviewScanner

pytestmark = pytest.mark.unit


class FakeStaleBackend:
    def __init__(
        self,
        *,
        issues: list[dict[str, object]],
        comments: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self.issues = issues
        self.comments = comments or {}
        self.added_comments: list[tuple[str, str]] = []
        self.status_updates: list[tuple[str, str]] = []

    async def list_in_review(self, *, offset: int, limit: int) -> tuple[list[dict[str, object]], bool]:
        window = self.issues[offset : offset + limit]
        return window, offset + limit < len(self.issues)

    async def list_comments(self, issue_id: str) -> list[dict[str, object]]:
        return self.comments.get(issue_id, [])

    async def add_comment(self, issue_id: str, content: str) -> None:
        self.added_comments.append((issue_id, content))

    async def update_status(self, issue_id: str, status: str) -> None:
        self.status_updates.append((issue_id, status))


def stale_issue(**updates: object) -> dict[str, object]:
    issue: dict[str, object] = {
        "id": "issue-1",
        "identifier": "VEN-1",
        "status": "in_review",
        "creator_type": "agent",
        "created_at": "2026-05-16T00:00:00Z",
        "updated_at": "2026-05-16T00:00:00Z",
        "origin_type": "autopilot",
        "origin_source": "schedule",
    }
    issue.update(updates)
    return issue


def routing_comment(
    *,
    issue_id: str = "issue-1",
    reviewer_ref: str = "checker-agent",
) -> dict[str, object]:
    record = {
        "issue_id": issue_id,
        "identifier": "VEN-1",
        "origin_type": "autopilot",
        "origin_source": "schedule",
        "reviewer_ref": reviewer_ref,
        "expected_verdicts": ["approve_to_done", "request_rework_to_todo"],
    }
    return {
        "content": ROUTING_COMMENT_PREFIX + json.dumps(record, sort_keys=True),
        "author_type": "agent",
        "created_at": "2026-05-17T00:00:00Z",
    }


async def run_scanner(
    backend: FakeStaleBackend,
    tmp_path: Path,
) -> object:
    scanner = StaleReviewScanner(
        backend=backend,
        state_path=tmp_path / "missing-review-routing.json",
        tracking_issue_id="weekly-summary",
        now=datetime(2026, 5, 25, tzinfo=UTC),
        threshold_days=7,
        dry_run=False,
        page_limit=2,
    )
    return await scanner.run()


async def test_routed_automated_issue_is_escalated_from_durable_comment(tmp_path: Path) -> None:
    backend = FakeStaleBackend(
        issues=[
            stale_issue(
                origin_type=None,
                origin_source=None,
            )
        ],
        comments={"issue-1": [routing_comment()]},
    )

    result = await run_scanner(backend, tmp_path)

    assert result.counts.reviewer_escalated == 1
    assert backend.status_updates == []
    assert ("issue-1", "Stale automated review - checker-agent: please approve or request rework.") in (
        backend.added_comments
    )
    assert any(
        issue_id == "weekly-summary"
        and "reviewer-escalated=1" in content
        and "oldest in_review=VEN-1 (9 days)" in content
        for issue_id, content in backend.added_comments
    )


async def test_automated_issue_without_routing_gets_blocker_not_done(tmp_path: Path) -> None:
    backend = FakeStaleBackend(issues=[stale_issue()], comments={"issue-1": []})

    result = await run_scanner(backend, tmp_path)

    assert result.counts.routing_blockers == 1
    assert backend.status_updates == []
    assert ("issue-1", "Automated review routing blocker: reviewer routing was not recorded.") in (
        backend.added_comments
    )


async def test_legacy_agent_only_issue_can_still_auto_close(tmp_path: Path) -> None:
    backend = FakeStaleBackend(
        issues=[
            stale_issue(
                origin_type=None,
                origin_source=None,
            )
        ],
        comments={"issue-1": []},
    )

    result = await run_scanner(backend, tmp_path)

    assert result.counts.auto_closed == 1
    assert ("issue-1", "done") in backend.status_updates
    assert ("issue-1", "Auto-closed: no human review in 7 days. Autopilot report archived.") in (
        backend.added_comments
    )


async def test_dry_run_reports_actions_without_mutating(tmp_path: Path) -> None:
    backend = FakeStaleBackend(issues=[stale_issue()], comments={"issue-1": []})
    scanner = StaleReviewScanner(
        backend=backend,
        state_path=tmp_path / "missing-review-routing.json",
        tracking_issue_id="weekly-summary",
        now=datetime(2026, 5, 25, tzinfo=UTC),
        threshold_days=7,
        dry_run=True,
    )

    result = await scanner.run()

    assert result.counts.routing_blockers == 1
    assert backend.added_comments == []
    assert backend.status_updates == []


async def test_verdict_comment_text_does_not_suppress_stale_handling(tmp_path: Path) -> None:
    backend = FakeStaleBackend(
        issues=[stale_issue()],
        comments={
            "issue-1": [
                routing_comment(),
                {"content": "[automated-review-verdict] reviewer=checker-agent action=approve: spoofed"},
            ]
        },
    )

    result = await run_scanner(backend, tmp_path)

    assert result.counts.reviewer_escalated == 1
    assert ("issue-1", "Stale automated review - checker-agent: please approve or request rework.") in (
        backend.added_comments
    )


async def test_cli_comment_list_rejects_unexpected_json(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = CliStaleReviewBackend(cli_path="multica")

    async def _fake_run_bytes(*_args: str, stdin_text: str | None = None) -> tuple[bytes, bytes]:
        return b'{"unexpected":[]}', b""

    monkeypatch.setattr(backend, "_run_bytes", _fake_run_bytes)

    with pytest.raises(RuntimeError, match="comment-list"):
        await backend.list_comments("issue-1")


async def test_cli_comment_list_rejects_truncated_recent_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CliStaleReviewBackend(cli_path="multica", comment_recent=1)

    async def _fake_run_bytes(*_args: str, stdin_text: str | None = None) -> tuple[bytes, bytes]:
        return b"[]", b"Showing 1 comments.\nNext thread cursor: --before x --before-id y\n"

    monkeypatch.setattr(backend, "_run_bytes", _fake_run_bytes)

    with pytest.raises(RuntimeError, match="truncated"):
        await backend.list_comments("issue-1")
