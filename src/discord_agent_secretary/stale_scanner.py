"""Production scanner for stale Multica issues left in review."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from .config import get_settings
from .review_router import ROUTING_COMMENT_PREFIX, VERDICT_COMMENT_PREFIX
from .stale_review import StaleScanCounts, classify_stale_in_review, format_stale_summary

HUMAN_AUTHOR_TYPES = frozenset({"member", "user", "human"})


class StaleReviewBackend(Protocol):
    async def list_in_review(self, *, offset: int, limit: int) -> tuple[list[dict[str, object]], bool]:
        ...

    async def list_comments(self, issue_id: str) -> list[dict[str, object]]:
        ...

    async def add_comment(self, issue_id: str, content: str) -> None:
        ...

    async def update_status(self, issue_id: str, status: str) -> None:
        ...


@dataclass(frozen=True)
class StaleIssueAction:
    issue_id: str
    identifier: str
    action: str
    dry_run: bool
    status_target: str | None = None


@dataclass(frozen=True)
class StaleScanResult:
    scanned: int
    counts: StaleScanCounts
    oldest_identifier: str | None
    oldest_age_days: int | None
    summary: str
    actions: list[StaleIssueAction]


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _strip_preamble(raw: str) -> str:
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("{", "[")):
            return "\n".join(lines[index:])
    return raw


def _parse_datetime(value: object) -> datetime | None:
    raw = _text(value)
    if raw is None:
        return None
    normalized = raw.removesuffix("Z") + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_days(issue: Mapping[str, object], now: datetime) -> int:
    value = issue.get("age_days")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    for key in ("in_review_since", "status_changed_at", "status_updated_at", "updated_at", "created_at"):
        started = _parse_datetime(issue.get(key))
        if started is not None:
            return max(0, int((now - started).total_seconds() // 86400))
    return 0


def _comment_content(comment: Mapping[str, object]) -> str:
    return _text(comment.get("content")) or ""


def _comment_author_type(comment: Mapping[str, object]) -> str | None:
    for key in ("author_type", "creator_type", "actor_type"):
        value = _text(comment.get(key))
        if value is not None:
            return value.lower()
    author = comment.get("author")
    if isinstance(author, Mapping):
        value = _text(author.get("type"))
        if value is not None:
            return value.lower()
    return None


def _has_human_comment(comments: list[dict[str, object]]) -> bool:
    return any(_comment_author_type(comment) in HUMAN_AUTHOR_TYPES for comment in comments)


def _has_verdict_comment(comments: list[dict[str, object]]) -> bool:
    return any(_comment_content(comment).startswith(VERDICT_COMMENT_PREFIX) for comment in comments)


def _parse_routing_record(comment: Mapping[str, object]) -> dict[str, object] | None:
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
    return record if _text(record.get("issue_id")) else None


def _load_state_routes(state_path: Path | None) -> dict[str, dict[str, object]]:
    if state_path is None:
        return {}
    try:
        data: Any = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        return {}
    issues = data.get("issues")
    if not isinstance(issues, dict):
        return {}
    routes: dict[str, dict[str, object]] = {}
    for issue_id, record in issues.items():
        if isinstance(issue_id, str) and isinstance(record, dict):
            routes[issue_id] = {
                str(key): value for key, value in record.items() if isinstance(key, str)
            }
    return routes


def _merge_comment_routes(
    routes: dict[str, dict[str, object]],
    comments: list[dict[str, object]],
) -> None:
    for comment in comments:
        record = _parse_routing_record(comment)
        if record is None:
            continue
        issue_id = _text(record.get("issue_id"))
        if issue_id is not None and issue_id not in routes:
            routes[issue_id] = record


def _enrich_issue(
    issue: Mapping[str, object],
    *,
    route: Mapping[str, object] | None,
    comments: list[dict[str, object]],
    now: datetime,
) -> dict[str, object]:
    enriched = dict(issue)
    enriched["age_days"] = _age_days(issue, now)
    enriched["human_comment_exists"] = _has_human_comment(comments)
    enriched["review_verdict_exists"] = _has_verdict_comment(comments)
    if route is not None:
        if _text(enriched.get("origin_type")) is None:
            enriched["origin_type"] = _text(route.get("origin_type")) or "autopilot"
        if _text(enriched.get("origin_source")) is None:
            enriched["origin_source"] = _text(route.get("origin_source")) or "schedule"
        for key in ("origin_id", "producer_agent_id", "reviewer_ref"):
            if enriched.get(key) is None and route.get(key) is not None:
                enriched[key] = route[key]
    return enriched


def _identifier(issue: Mapping[str, object]) -> str:
    return _text(issue.get("identifier")) or _text(issue.get("id")) or "unknown"


class StaleReviewScanner:
    def __init__(
        self,
        *,
        backend: StaleReviewBackend,
        state_path: Path | None,
        tracking_issue_id: str | None,
        now: datetime | None = None,
        threshold_days: int = 7,
        dry_run: bool = True,
        page_limit: int = 100,
    ) -> None:
        self._backend = backend
        self._state_path = state_path
        self._tracking_issue_id = tracking_issue_id
        self._now = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        self._threshold_days = threshold_days
        self._dry_run = dry_run
        self._page_limit = page_limit

    async def _list_all_in_review(self) -> list[dict[str, object]]:
        offset = 0
        issues: list[dict[str, object]] = []
        while True:
            page, has_more = await self._backend.list_in_review(offset=offset, limit=self._page_limit)
            issues.extend(page)
            if not has_more or not page:
                return issues
            offset += len(page)

    async def run(self) -> StaleScanResult:
        issues = await self._list_all_in_review()
        routes = _load_state_routes(self._state_path)
        actions: list[StaleIssueAction] = []
        auto_closed = 0
        human_escalated = 0
        reviewer_escalated = 0
        routing_blockers = 0
        oldest_identifier: str | None = None
        oldest_age_days: int | None = None

        for issue in issues:
            issue_id = _text(issue.get("id"))
            if issue_id is None:
                continue
            age_days = _age_days(issue, self._now)
            if oldest_age_days is None or age_days > oldest_age_days:
                oldest_age_days = age_days
                oldest_identifier = _identifier(issue)

            comments = await self._backend.list_comments(issue_id)
            _merge_comment_routes(routes, comments)
            route = routes.get(issue_id)
            enriched = _enrich_issue(issue, route=route, comments=comments, now=self._now)

            if enriched.get("review_verdict_exists") is True:
                continue
            decision = classify_stale_in_review(
                enriched,
                routed_state=routes,
                threshold_days=self._threshold_days,
            )
            if decision.action == "skip":
                continue

            if decision.action == "auto_close":
                auto_closed += 1
            elif decision.action == "human_escalate":
                human_escalated += 1
            elif decision.action == "reviewer_escalate":
                reviewer_escalated += 1
            elif decision.action == "routing_blocker":
                routing_blockers += 1

            actions.append(
                StaleIssueAction(
                    issue_id=issue_id,
                    identifier=_identifier(issue),
                    action=decision.action,
                    dry_run=self._dry_run,
                    status_target=decision.status_target,
                )
            )
            if not self._dry_run:
                await self._backend.add_comment(issue_id, decision.comment)
                if decision.status_target is not None:
                    await self._backend.update_status(issue_id, decision.status_target)

        counts = StaleScanCounts(
            auto_closed=auto_closed,
            human_escalated=human_escalated,
            reviewer_escalated=reviewer_escalated,
            routing_blockers=routing_blockers,
        )
        summary = format_stale_summary(
            week_of=self._now.date(),
            counts=counts,
            oldest_identifier=oldest_identifier,
            oldest_age_days=oldest_age_days,
        )
        if self._tracking_issue_id and not self._dry_run:
            await self._backend.add_comment(self._tracking_issue_id, summary)

        return StaleScanResult(
            scanned=len(issues),
            counts=counts,
            oldest_identifier=oldest_identifier,
            oldest_age_days=oldest_age_days,
            summary=summary,
            actions=actions,
        )


class CliStaleReviewBackend:
    def __init__(self, *, cli_path: str, timeout: float = 30.0, comment_recent: int = 200) -> None:
        self._cli_path = cli_path
        self._timeout = timeout
        self._comment_recent = comment_recent

    async def _run_json(self, *args: str, stdin_text: str | None = None) -> Any:
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
        text = _strip_preamble(stdout.decode("utf-8", errors="replace")).strip()
        if not text:
            return None
        return json.loads(text)

    async def list_in_review(self, *, offset: int, limit: int) -> tuple[list[dict[str, object]], bool]:
        data: Any = await self._run_json(
            "issue",
            "list",
            "--status",
            "in_review",
            "--limit",
            str(limit),
            "--offset",
            str(offset),
            "--output",
            "json",
        )
        if isinstance(data, dict):
            raw_issues = data.get("issues")
            has_more = data.get("has_more") is True
        elif isinstance(data, list):
            raw_issues = data
            has_more = len(data) >= limit
        else:
            raw_issues = []
            has_more = False
        if not isinstance(raw_issues, list):
            raw_issues = []
        issues = [dict(item) for item in raw_issues if isinstance(item, dict)]
        return cast(list[dict[str, object]], issues), has_more

    async def list_comments(self, issue_id: str) -> list[dict[str, object]]:
        data: Any = await self._run_json(
            "issue",
            "comment",
            "list",
            issue_id,
            "--output",
            "json",
            "--recent",
            str(self._comment_recent),
        )
        if isinstance(data, dict):
            raw_comments = data.get("comments", [])
        elif isinstance(data, list):
            raw_comments = data
        else:
            raw_comments = []
        comments = [dict(item) for item in raw_comments if isinstance(item, dict)]
        return cast(list[dict[str, object]], comments)

    async def add_comment(self, issue_id: str, content: str) -> None:
        await self._run_json(
            "issue",
            "comment",
            "add",
            issue_id,
            "--content-stdin",
            "--output",
            "json",
            stdin_text=content,
        )

    async def update_status(self, issue_id: str, status: str) -> None:
        await self._run_json(
            "issue",
            "status",
            issue_id,
            status,
            "--output",
            "json",
        )


def _parse_week_of(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan stale Multica in_review issues.")
    parser.add_argument("--tracking-issue", default=os.getenv("MULTICA_STALE_REVIEW_TRACKING_ISSUE"))
    parser.add_argument("--threshold-days", type=int, default=7)
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--comment-recent", type=int, default=200)
    parser.add_argument("--state-path")
    parser.add_argument("--cli-path")
    parser.add_argument("--week-of")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    parser.add_argument("--mutate", dest="dry_run", action="store_false")
    return parser


async def _amain(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    cli_path = args.cli_path or settings.multica_cli_path or "multica"
    state_path = Path(args.state_path or settings.multica_review_state_path)
    dry_run = settings.multica_review_dry_run if args.dry_run is None else bool(args.dry_run)
    week_of = _parse_week_of(args.week_of)
    now = datetime.combine(week_of, datetime.min.time(), tzinfo=UTC) if week_of else datetime.now(UTC)
    backend = CliStaleReviewBackend(
        cli_path=cli_path,
        timeout=settings.multica_cli_timeout,
        comment_recent=args.comment_recent,
    )
    scanner = StaleReviewScanner(
        backend=backend,
        state_path=state_path,
        tracking_issue_id=args.tracking_issue,
        now=now,
        threshold_days=args.threshold_days,
        dry_run=dry_run,
        page_limit=args.page_limit,
    )
    result = await scanner.run()
    print(
        json.dumps(
            {
                "dry_run": dry_run,
                "scanned": result.scanned,
                "counts": asdict(result.counts),
                "oldest_identifier": result.oldest_identifier,
                "oldest_age_days": result.oldest_age_days,
                "summary": result.summary,
                "actions": [asdict(action) for action in result.actions],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
