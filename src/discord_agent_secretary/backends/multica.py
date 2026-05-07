"""Multica CLI backend.

Async wrapper around the `multica` CLI binary. Uses asyncio's argv-form
process spawn (argv list, never a shell string) so untrusted arguments
cannot perform command injection. All calls route through a single
`_invoke` helper that enforces a timeout and raises typed errors.

This module owns Multica-specific error subtypes (`MulticaCliError`,
`MulticaCliTimeoutError`, `MulticaParseError`); each inherits the corresponding
backend-agnostic class in `backends.base`, so handlers catch the generic
type and logs retain the specific detail.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Final

from .base import (
    BackendCallError,
    BackendParseError,
    BackendTimeoutError,
    IssueBackendBase,
    IssueRef,
)


class MulticaCliTimeoutError(BackendTimeoutError):
    """The `multica` CLI did not exit before the configured timeout."""


class MulticaCliError(BackendCallError):
    """The `multica` CLI exited non-zero."""

    def __init__(self, exit_code: int, stderr: str) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"multica exited {exit_code}: {stderr.strip()}")


class MulticaParseError(BackendParseError):
    """The CLI output was not the expected JSON shape."""


_DEFAULT_CLI: Final = "multica"
_DEFAULT_TIMEOUT: Final = 8.0


def _strip_preamble(raw: str) -> str:
    """Drop leading non-JSON lines (e.g. 'Showing 5 of 12 items.')."""
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("{", "[")):
            return "\n".join(lines[i:])
    return raw


def _parse_json_output(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    body = _strip_preamble(text).strip()
    if not body:
        raise MulticaParseError("empty CLI output")
    try:
        return json.loads(body)
    except json.JSONDecodeError as err:
        raise MulticaParseError(f"invalid JSON from CLI: {err.msg}") from err


def _to_issue_ref(data: Any) -> IssueRef:
    if not isinstance(data, dict):
        raise MulticaParseError(f"expected JSON object, got {type(data).__name__}")
    issue_id = data.get("id")
    if not isinstance(issue_id, str) or not issue_id:
        raise MulticaParseError("missing 'id' in CLI response")
    return IssueRef(
        id=issue_id,
        status=data.get("status") if isinstance(data.get("status"), str) else None,
        title=data.get("title") if isinstance(data.get("title"), str) else None,
    )


class MulticaBackend(IssueBackendBase):
    """`IssueBackend` implementation backed by the `multica` CLI.

    Instantiate once per process; safe across coroutines since each call
    spawns an isolated child process.
    """

    def __init__(
        self,
        *,
        cli_path: str = _DEFAULT_CLI,
        workspace_id: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._cli_path = cli_path or _DEFAULT_CLI
        self._workspace_id = workspace_id
        self._timeout = timeout

    async def _invoke(self, *args: str) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            self._cli_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except TimeoutError as err:
            proc.kill()
            # Reap the child so it doesn't linger as a zombie. Bound the
            # wait so a process ignoring SIGKILL can't hang us forever.
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                pass
            raise MulticaCliTimeoutError(
                f"multica CLI timed out after {self._timeout}s"
            ) from err

        if proc.returncode is None:
            # communicate() returned but the child somehow has no exit
            # status — treat as a CLI failure rather than silently OK'ing.
            raise MulticaCliError(
                exit_code=-1,
                stderr="multica CLI returned without an exit status",
            )
        if proc.returncode != 0:
            raise MulticaCliError(
                exit_code=proc.returncode,
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        return stdout

    async def create_issue(
        self,
        title: str,
        *,
        description: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
    ) -> IssueRef:
        args = ["issue", "create", "--title", title, "--output", "json"]
        if description:
            args += ["--description", description]
        if priority:
            args += ["--priority", priority]
        if assignee:
            args += ["--assignee", assignee]
        raw = await self._invoke(*args)
        return _to_issue_ref(_parse_json_output(raw))

    async def get_issue(self, issue_id: str) -> IssueRef:
        raw = await self._invoke("issue", "get", issue_id, "--output", "json")
        return _to_issue_ref(_parse_json_output(raw))

    async def assign_issue(self, issue_id: str, to: str) -> IssueRef:
        raw = await self._invoke(
            "issue", "assign", issue_id, "--to", to, "--output", "json"
        )
        return _to_issue_ref(_parse_json_output(raw))

    async def update_status(self, issue_id: str, status: str) -> IssueRef:
        raw = await self._invoke(
            "issue", "status", issue_id, status, "--output", "json"
        )
        return _to_issue_ref(_parse_json_output(raw))
