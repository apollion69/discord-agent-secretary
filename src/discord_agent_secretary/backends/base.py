"""Backend protocol for issue trackers.

A backend converts Discord intents (`/task`, `/status`, `/assign`) into actions
on a concrete issue tracker (Multica, GitHub, Linear, Jira, ...). Handlers
depend on the protocol — never on a specific backend — so a deployment swaps
trackers by changing one env var (`BACKEND`).

Design:
  * `IssueRef` is a frozen dataclass — pass it around freely.
  * `IssueBackend` is a `typing.Protocol` — backends implement it structurally,
    no inheritance required (but encouraged via the `IssueBackendBase` ABC).
  * The error hierarchy (`BackendError`, `BackendTimeoutError`, `BackendCallError`,
    `BackendParseError`, `CircuitOpenError`) is backend-agnostic. Concrete
    backends raise subclasses (e.g. `MulticaCliError(BackendCallError)`)
    so handlers can `except BackendTimeoutError` while logs retain
    backend-specific detail.
  * `with_retry` and `CircuitBreaker` are reusable helpers — they live here
    so every backend gets the same semantics for free. Backends are
    expected to apply `with_retry` only to idempotent operations
    (`get_issue`, `update_status`, `assign_issue`); `create_issue` must
    not retry because a partial first attempt may have created an issue
    already.
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar, runtime_checkable

_T = TypeVar("_T")
_logger = logging.getLogger(__name__)


class BackendError(Exception):
    """Base class for any failure surfaced by an `IssueBackend`."""


class BackendTimeoutError(BackendError):
    """The backend did not respond within its configured timeout."""


class BackendCallError(BackendError):
    """The backend rejected the call (non-zero exit, HTTP 4xx/5xx, etc.)."""


class BackendParseError(BackendError):
    """The backend response could not be parsed into the expected shape."""


class CircuitOpenError(BackendError):
    """The circuit breaker is open and is fast-failing calls to spare the
    downstream from a thundering-herd retry storm."""


async def with_retry(
    coro_factory: Callable[[], Awaitable[_T]],
    *,
    attempts: int = 2,
    initial_backoff: float = 0.3,
    backoff_multiplier: float = 2.0,
    max_backoff: float = 2.0,
    retry_on: tuple[type[Exception], ...] = (BackendTimeoutError,),
) -> _T:
    """Run an awaitable with bounded retries and exponential backoff.

    `coro_factory` is a no-arg callable so each retry gets a fresh coroutine
    (an awaited coroutine cannot be re-awaited). Only the listed exception
    types trigger a retry; everything else (including `KeyboardInterrupt`
    and `SystemExit`) propagates immediately. Each intermediate failure
    is logged at DEBUG so investigators can reconstruct retry sequences.
    """
    delay = initial_backoff
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await coro_factory()
        except retry_on as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            sleep_for = min(delay, max_backoff)
            _logger.debug(
                "retryable failure — backing off",
                extra={
                    "attempt": attempt + 1,
                    "of": attempts,
                    "sleep": sleep_for,
                    "detail": str(exc),
                    "exc_type": type(exc).__name__,
                },
            )
            await asyncio.sleep(sleep_for)
            delay *= backoff_multiplier
    if last_exc is None:
        raise RuntimeError("with_retry exited loop without an exception")
    raise last_exc


class CircuitState(StrEnum):
    """Three classic circuit-breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-backend circuit breaker with a half-open probe.

    After `failure_threshold` consecutive failures the circuit opens and
    fast-fails for `reset_timeout` seconds. The next call after the cool-down
    runs in half-open state — a success closes the circuit, another failure
    re-opens it. Successes inside `CLOSED` reset the failure counter.

    Concurrency model:
      * Each method (`before_call`, `on_success`, `on_failure`) runs
        atomically inside one event loop because none of them `await`.
      * The wider read-modify-write window — `before_call()` → spawn child →
        `on_failure()` — DOES straddle awaits. Concurrent in-flight calls
        can therefore over-trip the breaker by N (one per concurrent call
        that observed CLOSED before any of them recorded a failure).
        `failure_threshold` is best understood as a soft target.
      * Multi-thread use is unsupported. If a future deployment shares a
        breaker across threads, wrap each method body in a `threading.Lock`.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._state: CircuitState = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        return self._state

    def before_call(self) -> None:
        """Inspect state and raise `CircuitOpenError` if currently open."""
        if self._state == CircuitState.OPEN:
            if self._clock() - self._opened_at >= self.reset_timeout:
                self._state = CircuitState.HALF_OPEN
            else:
                raise CircuitOpenError(
                    "circuit is open — refusing call until cool-down expires"
                )

    def on_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED

    def on_failure(self) -> None:
        self._failures += 1
        if (
            self._state == CircuitState.HALF_OPEN
            or self._failures >= self.failure_threshold
        ):
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()


@dataclass(frozen=True)
class IssueRef:
    """Lightweight reference to an issue in any tracker."""

    id: str
    status: str | None = None
    title: str | None = None
    identifier: str | None = None


@runtime_checkable
class IssueBackend(Protocol):
    """Structural contract every backend must satisfy.

    Method signatures are duplicated in `IssueBackendBase` below — keep them
    in sync. `tests/unit/test_backends.py::TestProtocolAbcAlignment` will
    fail if they drift.
    """

    async def create_issue(
        self,
        title: str,
        *,
        description: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        on_behalf_of: str | None = None,
    ) -> IssueRef: ...

    async def get_issue(self, issue_id: str) -> IssueRef: ...

    async def assign_issue(self, issue_id: str, to: str) -> IssueRef: ...

    async def update_status(self, issue_id: str, status: str) -> IssueRef: ...


class IssueBackendBase(ABC):
    """Optional ABC for backends that prefer inheritance over structural duck-typing.

    Subclassing buys you `isinstance()` checks and a single source of abstract
    method signatures; structural conformance via `Protocol` works equally well.
    """

    @abstractmethod
    async def create_issue(
        self,
        title: str,
        *,
        description: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        on_behalf_of: str | None = None,
    ) -> IssueRef: ...

    @abstractmethod
    async def get_issue(self, issue_id: str) -> IssueRef: ...

    @abstractmethod
    async def assign_issue(self, issue_id: str, to: str) -> IssueRef: ...

    @abstractmethod
    async def update_status(self, issue_id: str, status: str) -> IssueRef: ...
