"""Automated Multica review routing and reviewer verdict helpers."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from .review_routing import classify_review_candidate

logger = logging.getLogger(__name__)

RoutingMode = Literal["off", "subscribe", "assign"]
ROUTING_COMMENT_PREFIX = "[automated-review-routing] "
VERDICT_COMMENT_PREFIX = "[automated-review-verdict]"


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _is_uuid_ref(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class ReviewBackend(Protocol):
    async def list_comments(self, issue_id: str) -> list[dict[str, object]]: ...

    async def add_subscriber(self, issue_id: str, reviewer_ref: str) -> None: ...

    async def add_comment(self, issue_id: str, content: str) -> None: ...

    async def assign_issue(self, issue_id: str, assignee_ref: str) -> None: ...

    async def update_status(self, issue_id: str, status: str) -> None: ...


@dataclass(frozen=True)
class ReviewRouteResult:
    issue_id: str
    outcome: str
    reviewer_ref: str | None = None


def _comment_content(comment: dict[str, object] | Any) -> str:
    if not isinstance(comment, dict):
        return ""
    return _text(comment.get("content")) or ""


def parse_routing_record_comment(comment: dict[str, object]) -> dict[str, object] | None:
    content = _comment_content(comment)
    if not content.startswith(ROUTING_COMMENT_PREFIX):
        return None
    payload = content[len(ROUTING_COMMENT_PREFIX) :].strip()
    try:
        parsed: Any = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    record = {str(key): value for key, value in parsed.items() if isinstance(key, str)}
    issue_id = _text(record.get("issue_id"))
    reviewer_ref = _text(record.get("reviewer_ref"))
    if issue_id is None or reviewer_ref is None:
        return None
    return record


def _strip_preamble(raw: str) -> str:
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("{", "[")):
            return "\n".join(lines[index:])
    return raw


def parse_comment_list_json(data: Any) -> list[dict[str, object]]:
    if isinstance(data, dict):
        raw_comments = data.get("comments")
    elif isinstance(data, list):
        raw_comments = data
    else:
        raise RuntimeError("unexpected multica comment-list JSON shape")
    if not isinstance(raw_comments, list):
        raise RuntimeError("unexpected multica comment-list comments shape")
    comments: list[dict[str, object]] = []
    for item in raw_comments:
        if not isinstance(item, dict):
            raise RuntimeError("unexpected multica comment-list item shape")
        comments.append({str(key): value for key, value in item.items() if isinstance(key, str)})
    return comments


class CliReviewBackend:
    """Multica CLI-backed review backend.

    Reviewer refs are passed to the existing CLI resolver (`--user`/`--to`), so
    production can use member names or agent names already accepted by Multica.
    """

    def __init__(self, cli_path: str, timeout: float = 30.0) -> None:
        self._cli_path = cli_path
        self._timeout = timeout

    async def _run(self, *args: str, stdin_text: str | None = None) -> tuple[bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            self._cli_path,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        input_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(input_bytes), timeout=self._timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:500]
            output = stdout.decode("utf-8", errors="replace").strip()[:200]
            raise RuntimeError(f"multica exit {proc.returncode}: {detail or output}")
        return stdout, stderr

    async def _run_json(self, *args: str, stdin_text: str | None = None) -> Any:
        stdout, _stderr = await self._run(*args, stdin_text=stdin_text)
        text = _strip_preamble(stdout.decode("utf-8", errors="replace")).strip()
        if not text:
            return None
        return json.loads(text)

    async def list_comments(self, issue_id: str) -> list[dict[str, object]]:
        data = await self._run_json(
            "issue",
            "comment",
            "list",
            issue_id,
            "--output",
            "json",
        )
        return parse_comment_list_json(data)

    async def add_subscriber(self, issue_id: str, reviewer_ref: str) -> None:
        ref_flag = "--user-id" if _is_uuid_ref(reviewer_ref) else "--user"
        await self._run(
            "issue",
            "subscriber",
            "add",
            issue_id,
            ref_flag,
            reviewer_ref,
            "--output",
            "json",
        )

    async def add_comment(self, issue_id: str, content: str) -> None:
        await self._run(
            "issue",
            "comment",
            "add",
            issue_id,
            "--content-stdin",
            "--output",
            "json",
            stdin_text=content,
        )

    async def assign_issue(self, issue_id: str, assignee_ref: str) -> None:
        ref_flag = "--to-id" if _is_uuid_ref(assignee_ref) else "--to"
        await self._run(
            "issue",
            "assign",
            issue_id,
            ref_flag,
            assignee_ref,
            "--output",
            "json",
        )

    async def update_status(self, issue_id: str, status: str) -> None:
        await self._run(
            "issue",
            "status",
            issue_id,
            status,
            "--output",
            "json",
        )


class AutomatedReviewRouter:
    def __init__(
        self,
        *,
        reviewer_refs: list[str],
        routing_mode: RoutingMode,
        rework_status: str,
        dry_run: bool,
        state_path: Path,
        backend: ReviewBackend,
    ) -> None:
        self._reviewer_refs = [r.strip() for r in reviewer_refs if r.strip()]
        self._routing_mode = routing_mode
        self._rework_status = rework_status
        self._dry_run = dry_run
        self._state_path = state_path
        self._backend = backend

    def load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except json.JSONDecodeError:
            logger.error(
                "automated review routing state is corrupt",
                extra={"state_path": str(self._state_path)},
            )
            raise
        issues = data.get("issues")
        if not isinstance(issues, dict):
            data["issues"] = {}
        return cast(dict[str, Any], data)

    def _save_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self._state_path)

    async def _hydrate_route_from_comments(
        self,
        issue_id: str,
        state: dict[str, Any],
    ) -> dict[str, object] | None:
        comments = await self._backend.list_comments(issue_id)
        for comment in comments:
            record = parse_routing_record_comment(comment)
            if record is None or _text(record.get("issue_id")) != issue_id:
                continue
            state["issues"][issue_id] = record
            self._save_state(state)
            logger.info(
                "automated review routing state hydrated",
                extra={
                    "issue_id": issue_id,
                    "reviewer_ref": _text(record.get("reviewer_ref")),
                },
            )
            return record
        return None

    def _select_reviewer(self) -> str | None:
        if not self._reviewer_refs:
            return None
        return self._reviewer_refs[0]

    async def route_issue(self, issue: dict[str, object]) -> ReviewRouteResult:
        issue_id_raw = issue.get("id")
        if not isinstance(issue_id_raw, str) or not issue_id_raw:
            return ReviewRouteResult(issue_id="", outcome="skipped_missing_issue_id")
        issue_id = issue_id_raw

        decision = classify_review_candidate(issue)
        if not decision.is_automated_autopilot:
            return ReviewRouteResult(issue_id=issue_id, outcome="skipped_not_automated")
        if self._routing_mode == "off":
            return ReviewRouteResult(issue_id=issue_id, outcome="routing_off")

        reviewer_ref = self._select_reviewer()
        if reviewer_ref is None:
            logger.warning(
                "automated review routing blocked",
                extra={
                    "issue_id": issue_id,
                    "identifier": issue.get("identifier"),
                    "reason": "missing_reviewer_config",
                },
            )
            return ReviewRouteResult(issue_id=issue_id, outcome="blocked_missing_reviewer")

        state = self.load_state()
        issues = state["issues"]
        if issue_id in issues:
            return ReviewRouteResult(
                issue_id=issue_id,
                outcome="already_routed",
                reviewer_ref=str(issues[issue_id].get("reviewer_ref") or reviewer_ref),
            )
        hydrated = await self._hydrate_route_from_comments(issue_id, state)
        if hydrated is not None:
            return ReviewRouteResult(
                issue_id=issue_id,
                outcome="already_routed",
                reviewer_ref=_text(hydrated.get("reviewer_ref")) or reviewer_ref,
            )

        producer_agent_id = None
        if issue.get("assignee_type") == "agent" and isinstance(issue.get("assignee_id"), str):
            producer_agent_id = str(issue["assignee_id"])

        record = {
            "issue_id": issue_id,
            "identifier": issue.get("identifier"),
            "origin_type": issue.get("origin_type"),
            "origin_id": issue.get("origin_id"),
            "origin_source": issue.get("origin_source"),
            "producer_agent_id": producer_agent_id,
            "reviewer_ref": reviewer_ref,
            "routing_mode": self._routing_mode,
            "expected_verdicts": [
                "approve_to_done",
                f"request_rework_to_{self._rework_status}",
            ],
            "routed_at": datetime.now(UTC).isoformat(),
        }
        comment = ROUTING_COMMENT_PREFIX + json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
        )

        if self._dry_run:
            logger.info("automated review routing dry-run", extra=record)
            return ReviewRouteResult(issue_id=issue_id, outcome="dry_run", reviewer_ref=reviewer_ref)

        await self._backend.add_subscriber(issue_id, reviewer_ref)
        if self._routing_mode == "assign":
            # Reassign to the reviewer BEFORE posting the routing comment. The
            # triggering comment then dispatches the reviewer (intended), not the
            # producer. In `subscribe` mode the producer stays the assignee and is
            # pinged into a no-op session by every routing/verdict comment — only
            # `assign` mode removes the producer from that wasteful loop.
            await self._backend.assign_issue(issue_id, reviewer_ref)
        await self._backend.add_comment(issue_id, comment)

        issues[issue_id] = record
        self._save_state(state)
        return ReviewRouteResult(issue_id=issue_id, outcome="routed", reviewer_ref=reviewer_ref)

    async def _validate_verdict_owner(
        self,
        issue_id: str,
        reviewer_ref: str,
    ) -> tuple[str | None, dict[str, object] | None]:
        state = self.load_state()
        issue_record = state["issues"].get(issue_id)
        if not isinstance(issue_record, dict):
            issue_record = await self._hydrate_route_from_comments(issue_id, state)
        if not isinstance(issue_record, dict):
            logger.warning(
                "automated review verdict rejected",
                extra={
                    "issue_id": issue_id,
                    "reviewer_ref": reviewer_ref,
                    "reason": "unrouted_issue",
                },
            )
            return "verdict_rejected_unrouted", None
        recorded_reviewer = _text(issue_record.get("reviewer_ref"))
        if recorded_reviewer != reviewer_ref:
            logger.warning(
                "automated review verdict rejected",
                extra={
                    "issue_id": issue_id,
                    "reviewer_ref": reviewer_ref,
                    "recorded_reviewer_ref": recorded_reviewer,
                    "reason": "wrong_reviewer",
                },
            )
            return "verdict_rejected_reviewer", None
        return None, cast(dict[str, object], issue_record)

    async def approve(
        self,
        issue_id: str,
        *,
        reviewer_ref: str,
        comment: str,
    ) -> ReviewRouteResult:
        content = comment.strip() or "approved"
        rejection, _issue_record = await self._validate_verdict_owner(issue_id, reviewer_ref)
        if rejection is not None:
            return ReviewRouteResult(issue_id=issue_id, outcome=rejection, reviewer_ref=reviewer_ref)
        if self._dry_run:
            logger.info(
                "automated review approve dry-run",
                extra={"issue_id": issue_id, "reviewer_ref": reviewer_ref},
            )
            return ReviewRouteResult(issue_id=issue_id, outcome="dry_run_approved", reviewer_ref=reviewer_ref)

        await self._backend.add_comment(
            issue_id,
            f"{VERDICT_COMMENT_PREFIX} reviewer={reviewer_ref} action=approve: {content}",
        )
        await self._backend.update_status(issue_id, "done")
        return ReviewRouteResult(issue_id=issue_id, outcome="approved", reviewer_ref=reviewer_ref)

    async def request_rework(
        self,
        issue_id: str,
        *,
        reviewer_ref: str,
        comment: str,
    ) -> ReviewRouteResult:
        content = comment.strip()
        if not content:
            logger.warning(
                "automated review rework rejected",
                extra={
                    "issue_id": issue_id,
                    "reviewer_ref": reviewer_ref,
                    "reason": "empty_comment",
                },
            )
            return ReviewRouteResult(
                issue_id=issue_id,
                outcome="rework_comment_required",
                reviewer_ref=reviewer_ref,
            )

        rejection, issue_record = await self._validate_verdict_owner(issue_id, reviewer_ref)
        if rejection is not None:
            return ReviewRouteResult(issue_id=issue_id, outcome=rejection, reviewer_ref=reviewer_ref)
        assert issue_record is not None
        producer_agent_id = issue_record.get("producer_agent_id")

        if self._dry_run:
            logger.info(
                "automated review rework dry-run",
                extra={"issue_id": issue_id, "reviewer_ref": reviewer_ref},
            )
            return ReviewRouteResult(
                issue_id=issue_id,
                outcome="dry_run_rework",
                reviewer_ref=reviewer_ref,
            )

        await self._backend.add_comment(
            issue_id,
            f"{VERDICT_COMMENT_PREFIX} reviewer={reviewer_ref} action=rework: {content}",
        )
        await self._backend.update_status(issue_id, self._rework_status)
        if isinstance(producer_agent_id, str) and producer_agent_id:
            await self._backend.assign_issue(issue_id, producer_agent_id)
        return ReviewRouteResult(
            issue_id=issue_id,
            outcome="rework_requested",
            reviewer_ref=reviewer_ref,
        )
