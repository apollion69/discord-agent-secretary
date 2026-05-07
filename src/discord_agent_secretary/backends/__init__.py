"""Issue-tracker backends.

Public surface:
  * `IssueBackend` — structural contract every backend implements.
  * `IssueBackendBase` — optional ABC alternative.
  * `IssueRef` — frozen dataclass for issue references.
  * Error hierarchy: `BackendError`, `BackendTimeoutError`, `BackendCallError`,
    `BackendParseError`, `CircuitOpenError`.
  * `with_retry`, `CircuitBreaker` — reusable resilience helpers.
  * `make_backend(settings)` — factory selecting a concrete backend by
    `settings.backend` value.

Adding a new backend is a five-step contribution; see
`CONTRIBUTING.md` → "Adding a backend".
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .base import (
    BackendCallError,
    BackendError,
    BackendParseError,
    BackendTimeoutError,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    IssueBackend,
    IssueBackendBase,
    IssueRef,
    with_retry,
)

if TYPE_CHECKING:
    from ..config import Settings

__all__ = [
    "BackendCallError",
    "BackendError",
    "BackendParseError",
    "BackendTimeoutError",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "IssueBackend",
    "IssueBackendBase",
    "IssueRef",
    "make_backend",
    "with_retry",
]


def _build_multica(settings: Settings) -> IssueBackend:
    from .multica import MulticaBackend

    return MulticaBackend(
        cli_path=settings.multica_cli_path or "multica",
        workspace_id=settings.multica_workspace_id,
        timeout=settings.multica_cli_timeout,
        output_byte_limit=settings.multica_cli_output_byte_limit,
        circuit_failure_threshold=settings.backend_circuit_failure_threshold,
        circuit_reset_timeout=settings.backend_circuit_reset_timeout,
    )


def _build_github(settings: Settings) -> IssueBackend:
    from .github import GitHubBackend

    return GitHubBackend(token=settings.github_token, repo=settings.github_repo)


def _build_linear(settings: Settings) -> IssueBackend:
    from .linear import LinearBackend

    return LinearBackend(
        api_key=settings.linear_api_key, team_id=settings.linear_team_id
    )


def _build_jira(settings: Settings) -> IssueBackend:
    from .jira import JiraBackend

    return JiraBackend(
        base_url=settings.jira_base_url,
        email=settings.jira_email,
        api_token=settings.jira_api_token,
        project_key=settings.jira_project_key,
    )


_BACKEND_BUILDERS: dict[str, Callable[[Settings], IssueBackend]] = {
    "multica": _build_multica,
    "github": _build_github,
    "linear": _build_linear,
    "jira": _build_jira,
}


def make_backend(settings: Settings) -> IssueBackend:
    """Return a concrete `IssueBackend` selected by `settings.backend`.

    Raises `ValueError` for unknown backend names. Stub backends raise
    `NotImplementedError` from their `__init__`, so a misconfigured
    deployment fails fast at boot rather than at the first slash command.
    """
    name = settings.backend.lower()
    builder = _BACKEND_BUILDERS.get(name)
    if builder is None:
        raise ValueError(
            f"unknown backend: {settings.backend!r} "
            f"(supported: {', '.join(sorted(_BACKEND_BUILDERS))})"
        )
    return builder(settings)
