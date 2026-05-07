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
    """Not implemented — see module docstring for contributor guide."""

    def __init__(self, *, api_key: str, team_id: str, timeout: float = 10.0) -> None:
        self._api_key = api_key
        self._team_id = team_id
        self._timeout = timeout

    async def create_issue(
        self,
        title: str,
        *,
        description: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
    ) -> IssueRef:
        raise NotImplementedError("LinearBackend.create_issue not implemented yet.")

    async def get_issue(self, issue_id: str) -> IssueRef:
        raise NotImplementedError("LinearBackend.get_issue not implemented yet.")

    async def assign_issue(self, issue_id: str, to: str) -> IssueRef:
        raise NotImplementedError("LinearBackend.assign_issue not implemented yet.")

    async def update_status(self, issue_id: str, status: str) -> IssueRef:
        raise NotImplementedError("LinearBackend.update_status not implemented yet.")
