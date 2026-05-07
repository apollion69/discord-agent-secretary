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
    `BackendParseError`) is backend-agnostic. Concrete backends raise
    subclasses (e.g. `MulticaCliError(BackendCallError)`) so handlers can
    `except BackendTimeoutError` while logs retain backend-specific detail.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class BackendError(Exception):
    """Base class for any failure surfaced by an `IssueBackend`."""


class BackendTimeoutError(BackendError):
    """The backend did not respond within its configured timeout."""


class BackendCallError(BackendError):
    """The backend rejected the call (non-zero exit, HTTP 4xx/5xx, etc.)."""


class BackendParseError(BackendError):
    """The backend response could not be parsed into the expected shape."""


@dataclass(frozen=True)
class IssueRef:
    """Lightweight reference to an issue in any tracker."""

    id: str
    status: str | None = None
    title: str | None = None


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
    ) -> IssueRef: ...

    @abstractmethod
    async def get_issue(self, issue_id: str) -> IssueRef: ...

    @abstractmethod
    async def assign_issue(self, issue_id: str, to: str) -> IssueRef: ...

    @abstractmethod
    async def update_status(self, issue_id: str, status: str) -> IssueRef: ...
