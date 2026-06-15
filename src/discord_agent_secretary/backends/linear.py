"""Linear backend — STUB.

Good first issue. Implement `IssueBackend` against Linear's GraphQL API:

  * Endpoint: https://api.linear.app/graphql
  * Auth: `Authorization: <api-key>` (personal API key from Linear settings)
  * `create_issue` → mutation `issueCreate(input: {title, description,
    teamId, priority, assigneeId})`
  * `get_issue`    → query `issue(id: $id) { id, identifier, title, state }`
  * `assign_issue` → mutation `issueUpdate(id: $id, input: {assigneeId})`
  * `update_status`→ mutation `issueUpdate(id: $id, input: {stateId})` —
    map our generic statuses (`todo`, `in_progress`, `done`, ...) to the
    target team's workflow state IDs (cache on first call).

Map GraphQL `errors[]` to `BackendCallError`, network timeouts to
`BackendTimeout`, malformed JSON / missing fields to `BackendParseError`.
"""
from __future__ import annotations

from .base import IssueBackendBase, IssueRef


class LinearBackend(IssueBackendBase):
    """Not implemented — see module docstring for contributor guide.

    Construction itself raises so a misconfigured `BACKEND=linear` deployment
    fails at boot, not on the first user-visible `/task`.
    """

    def __init__(self, *, api_key: str, team_id: str, timeout: float = 10.0) -> None:
        raise NotImplementedError(
            "LinearBackend is a stub — see module docstring for the contributor "
            "guide. Switch BACKEND to a supported value or open a PR."
        )

    async def create_issue(
        self,
        title: str,
        *,
        description: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        parent: str | None = None,
        on_behalf_of: str | None = None,
    ) -> IssueRef:  # pragma: no cover — unreachable, __init__ raises.
        raise NotImplementedError

    async def get_issue(self, issue_id: str) -> IssueRef:  # pragma: no cover
        raise NotImplementedError

    async def assign_issue(self, issue_id: str, to: str) -> IssueRef:  # pragma: no cover
        raise NotImplementedError

    async def update_status(self, issue_id: str, status: str) -> IssueRef:  # pragma: no cover
        raise NotImplementedError

    async def add_comment(
        self, issue_id: str, content: str, *, on_behalf_of: str | None = None
    ) -> None:  # pragma: no cover
        raise NotImplementedError
