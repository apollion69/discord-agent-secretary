"""Multica CLI backend.

Async wrapper around the `multica` CLI binary. Uses asyncio's argv-form
process spawn (argv list, never a shell string) so untrusted arguments
cannot perform command injection. All calls route through a single
`_invoke` helper that enforces a timeout, an output-size cap, a
circuit breaker, and (for idempotent calls only) a small bounded retry.

This module owns Multica-specific error subtypes (`MulticaCliError`,
`MulticaCliTimeoutError`, `MulticaParseError`); each inherits the corresponding
backend-agnostic class in `backends.base`, so handlers catch the generic
type and logs retain the specific detail.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Final

from .._cli import strip_preamble as _strip_preamble
from .base import (
    BackendCallError,
    BackendParseError,
    BackendTimeoutError,
    CircuitBreaker,
    IssueBackendBase,
    IssueRef,
    with_retry,
)


class MulticaCliTimeoutError(BackendTimeoutError):
    """The `multica` CLI did not exit before the configured timeout."""


class MulticaCliError(BackendCallError):
    """The `multica` CLI exited non-zero, was killed, or overflowed output."""

    def __init__(self, exit_code: int, stderr: str) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        stripped = stderr.strip()
        safe = stripped[:_STDERR_LOG_CAP] + ("…" if len(stripped) > _STDERR_LOG_CAP else "")
        super().__init__(f"multica exited {exit_code}: {safe}")


class MulticaParseError(BackendParseError):
    """The CLI output was not the expected JSON shape."""


_logger = logging.getLogger(__name__)

_DEFAULT_CLI: Final = "multica"
_DEFAULT_TIMEOUT: Final = 8.0
_DEFAULT_OUTPUT_LIMIT: Final = 10 * 1024 * 1024  # 10 MiB
_REAP_TIMEOUT: Final = 2.0
_STDERR_LOG_CAP: Final = 300  # chars; prevents 10 MiB CLI errors flooding logs


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
        identifier=data.get("identifier") if isinstance(data.get("identifier"), str) else None,
    )


async def _read_capped(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Read up to `limit + 1` bytes; flag overflow if the cap is exceeded."""
    data = await stream.read(limit + 1)
    return data, len(data) > limit


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Wait for a (likely killed) child to exit, capped so we can't hang."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT)
    except TimeoutError:
        _logger.warning(
            "multica subprocess still running after SIGKILL",
            extra={"pid": proc.pid, "reap_timeout": _REAP_TIMEOUT},
        )


class MulticaBackend(IssueBackendBase):
    """`IssueBackend` implementation backed by the `multica` CLI.

    Instantiate once per process; safe across coroutines since each call
    spawns an isolated child process. The instance also owns a single
    `CircuitBreaker` shared across coroutines — a quick stream of timeouts
    opens the circuit and subsequent calls fast-fail until the cool-down.
    """

    def __init__(
        self,
        *,
        cli_path: str = _DEFAULT_CLI,
        workspace_id: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
        output_byte_limit: int = _DEFAULT_OUTPUT_LIMIT,
        circuit_failure_threshold: int = 5,
        circuit_reset_timeout: float = 30.0,
    ) -> None:
        self._cli_path = cli_path or _DEFAULT_CLI
        self._workspace_id = workspace_id
        self._timeout = timeout
        self._output_byte_limit = output_byte_limit
        self._circuit = CircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            reset_timeout=circuit_reset_timeout,
        )

    @property
    def circuit(self) -> CircuitBreaker:
        return self._circuit

    async def _invoke(self, *args: str, env: dict[str, str] | None = None) -> bytes:
        # Fast-fail without spawning if the breaker is open.
        self._circuit.before_call()

        try:
            stdout = await self._spawn_and_read(*args, env=env)
        except (BackendTimeoutError, BackendCallError):
            self._circuit.on_failure()
            raise
        # Parse errors are raised later; success here means the CLI exited
        # cleanly with output under the cap, which is what the breaker cares
        # about.
        self._circuit.on_success()
        return stdout

    async def _spawn_and_read(self, *args: str, env: dict[str, str] | None = None) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            self._cli_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        # `proc.stdout` / `proc.stderr` are guaranteed non-None when both
        # are PIPE'd, but mypy can't narrow that — assert for clarity.
        assert proc.stdout is not None and proc.stderr is not None

        try:
            (stdout, stdout_over), (stderr, stderr_over) = await asyncio.wait_for(
                asyncio.gather(
                    _read_capped(proc.stdout, self._output_byte_limit),
                    _read_capped(proc.stderr, self._output_byte_limit),
                ),
                timeout=self._timeout,
            )
        except TimeoutError as err:
            proc.kill()
            await _reap(proc)
            raise MulticaCliTimeoutError(
                f"multica CLI timed out after {self._timeout}s"
            ) from err

        if stdout_over or stderr_over:
            proc.kill()
            await _reap(proc)
            raise MulticaCliError(
                exit_code=-1,
                stderr=(
                    f"multica CLI exceeded {self._output_byte_limit}-byte output cap"
                ),
            )

        await _reap(proc)
        if proc.returncode is None:
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

    def _parse_ref(self, raw: bytes) -> IssueRef:
        """Parse a CLI JSON response into an IssueRef.

        A malformed CLI response counts as a circuit-breaker failure: a
        persistently broken CLI path would otherwise drain calls indefinitely
        without ever opening the breaker (the spawn itself "succeeded").
        """
        try:
            return _to_issue_ref(_parse_json_output(raw))
        except MulticaParseError:
            self._circuit.on_failure()
            raise

    async def create_issue(
        self,
        title: str,
        *,
        description: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        on_behalf_of: str | None = None,
    ) -> IssueRef:
        # NOT retried: a partial first attempt may have created an issue —
        # retrying would risk duplicates. The circuit breaker still applies.
        args = ["issue", "create", "--title", title, "--output", "json"]
        if description:
            args += ["--description", description]
        if priority:
            args += ["--priority", priority]
        if assignee:
            args += ["--assignee", assignee]
        # Act-as-member: forward MULTICA_ON_BEHALF_OF only when set, so other
        # calls inherit the parent env unchanged (env=None means "inherit").
        env: dict[str, str] | None = None
        if on_behalf_of:
            env = {**os.environ, "MULTICA_ON_BEHALF_OF": on_behalf_of}
        raw = await self._invoke(*args, env=env)
        return self._parse_ref(raw)

    async def get_issue(self, issue_id: str) -> IssueRef:
        raw = await with_retry(
            lambda: self._invoke("issue", "get", issue_id, "--output", "json"),
            retry_on=(BackendTimeoutError,),
        )
        return self._parse_ref(raw)

    async def assign_issue(self, issue_id: str, to: str) -> IssueRef:
        raw = await with_retry(
            lambda: self._invoke(
                "issue", "assign", issue_id, "--to", to, "--output", "json"
            ),
            retry_on=(BackendTimeoutError,),
        )
        return self._parse_ref(raw)

    async def update_status(self, issue_id: str, status: str) -> IssueRef:
        raw = await with_retry(
            lambda: self._invoke(
                "issue", "status", issue_id, status, "--output", "json"
            ),
            retry_on=(BackendTimeoutError,),
        )
        return self._parse_ref(raw)

    async def add_comment(
        self, issue_id: str, content: str, *, on_behalf_of: str | None = None
    ) -> None:
        # NOT retried: a partial first attempt may have posted the comment —
        # retrying would risk a duplicate. The circuit breaker still applies.
        env: dict[str, str] | None = None
        if on_behalf_of:
            env = {**os.environ, "MULTICA_ON_BEHALF_OF": on_behalf_of}
        await self._invoke("issue", "comment", "add", issue_id, "--content", content, env=env)
