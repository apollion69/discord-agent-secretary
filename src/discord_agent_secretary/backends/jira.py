"""Jira backend — STUB.

Good first issue. Implement `IssueBackend` against Jira REST API v3:

  * Cloud:   https://{site}.atlassian.net/rest/api/3/issue
  * Server:  https://{host}/rest/api/2/issue   (note: v2 schema differs)
  * Auth: HTTP basic with email + API token (Cloud) or PAT (Server/DC).
  * `create_issue` → POST /rest/api/3/issue with `{fields: {project, summary,
    description (ADF), priority, assignee}}`
  * `get_issue`    → GET /rest/api/3/issue/{issueIdOrKey}
  * `assign_issue` → PUT /rest/api/3/issue/{issueIdOrKey}/assignee
                     (body: `{accountId}` for Cloud, `{name}` for Server)
  * `update_status`→ POST /rest/api/3/issue/{issueIdOrKey}/transitions —
                     resolve transition IDs by GET'ting available transitions,
                     match by `to.name`. Cache per project.

Jira description uses Atlassian Document Format on Cloud — wrap plain text
in a minimal ADF doc (`{type: "doc", version: 1, content: [...]}`). Server
v2 still accepts wiki markup or plain string.
"""
from __future__ import annotations

from .base import IssueBackendBase, IssueRef


class JiraBackend(IssueBackendBase):
    """Not implemented — see module docstring for contributor guide.

    Construction itself raises so a misconfigured `BACKEND=jira` deployment
    fails at boot, not on the first user-visible `/task`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str,
        timeout: float = 10.0,
    ) -> None:
        raise NotImplementedError(
            "JiraBackend is a stub — see module docstring for the contributor "
            "guide. Switch BACKEND to a supported value or open a PR."
        )

    async def create_issue(
        self,
        title: str,
        *,
        description: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
    ) -> IssueRef:  # pragma: no cover — unreachable, __init__ raises.
        raise NotImplementedError

    async def get_issue(self, issue_id: str) -> IssueRef:  # pragma: no cover
        raise NotImplementedError

    async def assign_issue(self, issue_id: str, to: str) -> IssueRef:  # pragma: no cover
        raise NotImplementedError

    async def update_status(self, issue_id: str, status: str) -> IssueRef:  # pragma: no cover
        raise NotImplementedError
