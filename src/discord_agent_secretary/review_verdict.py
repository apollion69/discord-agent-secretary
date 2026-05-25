"""CLI entrypoint for automated Multica review routing verdicts."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol, cast

from .config import get_settings
from .review_router import AutomatedReviewRouter, CliReviewBackend


class ReviewActionRouter(Protocol):
    async def route_issue(self, issue: dict[str, object]) -> Any: ...

    async def approve(self, issue_id: str, *, reviewer_ref: str, comment: str) -> Any: ...

    async def request_rework(self, issue_id: str, *, reviewer_ref: str, comment: str) -> Any: ...


def _strip_preamble(raw: str) -> str:
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("{", "[")):
            return "\n".join(lines[index:])
    return raw


async def _run_cli_json(cli_path: str, cli_timeout: float, *args: str) -> Any:
    proc = await asyncio.create_subprocess_exec(
        cli_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=cli_timeout)
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


async def _load_issue(cli_path: str, cli_timeout: float, issue_ref: str) -> dict[str, object]:
    data = await _run_cli_json(cli_path, cli_timeout, "issue", "get", issue_ref, "--output", "json")
    if not isinstance(data, dict):
        raise RuntimeError("unexpected multica issue-get JSON shape")
    return {str(key): value for key, value in data.items() if isinstance(key, str)}


async def execute_review_action(
    *,
    router: ReviewActionRouter,
    issue: dict[str, object],
    action: str,
    reviewer_ref: str,
    comment: str,
) -> dict[str, object]:
    issue_id = issue.get("id")
    if not isinstance(issue_id, str) or not issue_id:
        raise RuntimeError("issue payload does not contain an id")

    if action == "route":
        result = await router.route_issue(issue)
    elif action == "approve":
        result = await router.approve(issue_id, reviewer_ref=reviewer_ref, comment=comment)
    elif action == "rework":
        result = await router.request_rework(issue_id, reviewer_ref=reviewer_ref, comment=comment)
    else:
        raise RuntimeError(f"unsupported action: {action}")

    return cast(dict[str, object], asdict(result))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Route or review an automated Multica issue.")
    parser.add_argument("issue", help="Multica issue identifier or UUID")
    parser.add_argument("action", choices=["route", "approve", "rework"])
    parser.add_argument("--reviewer", default="", help="Reviewer actor ref/name; defaults to first configured reviewer")
    parser.add_argument("--comment", default="", help="Reviewer verdict comment")
    parser.add_argument("--cli-path", default="")
    parser.add_argument("--state-path", default="")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    parser.add_argument("--mutate", dest="dry_run", action="store_false")
    return parser


async def _amain(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    cli_path = args.cli_path or settings.multica_cli_path or "multica"
    reviewer_ref = args.reviewer or (
        settings.multica_automated_reviewers[0] if settings.multica_automated_reviewers else ""
    )
    if not reviewer_ref:
        parser.error("--reviewer is required when MULTICA_AUTOMATED_REVIEWERS is empty")
    if args.action == "rework" and not args.comment.strip():
        parser.error("--comment is required for rework")

    dry_run = settings.multica_review_dry_run if args.dry_run is None else bool(args.dry_run)
    router = AutomatedReviewRouter(
        reviewer_refs=[reviewer_ref],
        routing_mode=settings.multica_review_routing_mode,
        rework_status=settings.multica_rework_status,
        dry_run=dry_run,
        state_path=Path(args.state_path or settings.multica_review_state_path),
        backend=CliReviewBackend(cli_path, timeout=max(settings.multica_cli_timeout, 30.0)),
    )
    issue = await _load_issue(cli_path, max(settings.multica_cli_timeout, 30.0), args.issue)
    result = await execute_review_action(
        router=router,
        issue=issue,
        action=args.action,
        reviewer_ref=reviewer_ref,
        comment=args.comment,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_amain(argv))
    except Exception as exc:
        print(f"review verdict failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
