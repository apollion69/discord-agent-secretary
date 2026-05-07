"""Issue-tracker backends.

Public surface:
  * `IssueBackend` — structural contract every backend implements.
  * `IssueBackendBase` — optional ABC alternative.
  * `IssueRef` — frozen dataclass for issue references.
  * Error hierarchy: `BackendError`, `BackendTimeoutError`, `BackendCallError`,
    `BackendParseError`.
  * `make_backend(settings)` — factory selecting a concrete backend by
    `settings.backend` value.

Adding a new backend is a five-step contribution; see
`CONTRIBUTING.md` → "Adding a backend".
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import (
    BackendCallError,
    BackendError,
    BackendParseError,
    BackendTimeoutError,
    IssueBackend,
    IssueBackendBase,
    IssueRef,
)

if TYPE_CHECKING:
    from ..config import Settings

__all__ = [
    "BackendCallError",
    "BackendError",
    "BackendParseError",
    "BackendTimeoutError",
    "IssueBackend",
    "IssueBackendBase",
    "IssueRef",
    "make_backend",
]


def make_backend(settings: Settings) -> IssueBackend:
    """Return a concrete `IssueBackend` selected by `settings.backend`.

    Raises `ValueError` for unknown backend names. Stub backends raise
    `NotImplementedError` from their `__init__`, so a misconfigured
    deployment fails fast at boot rather than at the first slash command.
    """
    name = settings.backend.lower()

    if name == "multica":
        from .multica import MulticaBackend

        return MulticaBackend(
            cli_path=settings.multica_cli_path or "multica",
            workspace_id=settings.multica_workspace_id,
            timeout=settings.multica_cli_timeout,
        )

    if name == "github":
        from .github import GitHubBackend

        return GitHubBackend(
            token=settings.github_token,
            repo=settings.github_repo,
        )

    if name == "linear":
        from .linear import LinearBackend

        return LinearBackend(
            api_key=settings.linear_api_key,
            team_id=settings.linear_team_id,
        )

    if name == "jira":
        from .jira import JiraBackend

        return JiraBackend(
            base_url=settings.jira_base_url,
            email=settings.jira_email,
            api_token=settings.jira_api_token,
            project_key=settings.jira_project_key,
        )

    raise ValueError(
        f"unknown backend: {settings.backend!r} "
        "(supported: multica, github, linear, jira)"
    )
