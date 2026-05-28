"""GitHub Issues backend — STUB.

Good first issue for contributors. Implement `IssueBackend` against
the GitHub REST API:

  * `create_issue` → POST /repos/{owner}/{repo}/issues
    https://docs.github.com/en/rest/issues/issues#create-an-issue
  * `get_issue`    → GET  /repos/{owner}/{repo}/issues/{issue_number}
  * `assign_issue` → POST /repos/{owner}/{repo}/issues/{issue_number}/assignees
  * `update_status`→ PATCH /repos/{owner}/{repo}/issues/{issue_number}
                     (`state: open|closed`; map "done"/"blocked" to closed,
                     others to open + a label like `status:in-progress`)

Auth: GitHub fine-grained PAT or App-installation token via
  `GITHUB_TOKEN` env var. Honour `Retry-After` on 403 rate-limit responses.

Map GitHub HTTP errors to `BackendCallError`; timeouts to `BackendTimeout`;
schema mismatches to `BackendParseError`.
"""
from __future__ import annotations

from .base import IssueBackendBase, IssueRef


class GitHubBackend(IssueBackendBase):
    """Not implemented — see module docstring for contributor guide.

    Construction itself raises so a misconfigured `BACKEND=github` deployment
    fails at boot, not on the first user-visible `/task`.
    """

    def __init__(self, *, token: str, repo: str, timeout: float = 10.0) -> None:
        raise NotImplementedError(
            "GitHubBackend is a stub — see module docstring for the contributor "
            "guide. Switch BACKEND to a supported value or open a PR."
        )

    async def create_issue(
        self,
        title: str,
        *,
        description: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        on_behalf_of: str | None = None,
    ) -> IssueRef:  # pragma: no cover — unreachable, __init__ raises.
        raise NotImplementedError

    async def get_issue(self, issue_id: str) -> IssueRef:  # pragma: no cover
        raise NotImplementedError

    async def assign_issue(self, issue_id: str, to: str) -> IssueRef:  # pragma: no cover
        raise NotImplementedError

    async def update_status(self, issue_id: str, status: str) -> IssueRef:  # pragma: no cover
        raise NotImplementedError
