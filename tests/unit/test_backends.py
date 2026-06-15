"""Unit tests for the backend protocol + factory.

Covers:
  * `IssueBackend` is a runtime-checkable Protocol — `MulticaBackend`
    structurally satisfies it, plain `object` does not.
  * `make_backend(settings)` routes by `settings.backend`.
  * Stub backends (GitHub / Linear / Jira) raise `NotImplementedError` on
    first call (not at construction) so misconfigurations surface clearly.
  * Concrete backend errors (`MulticaCliError` etc.) inherit the abstract
    `BackendError` family so handlers can catch the abstract type.
"""
from __future__ import annotations

import pytest

from discord_agent_secretary.backends import (
    BackendCallError,
    BackendError,
    BackendParseError,
    BackendTimeoutError,
    IssueBackend,
    IssueBackendBase,
    IssueRef,
    make_backend,
)
from discord_agent_secretary.backends.github import GitHubBackend
from discord_agent_secretary.backends.jira import JiraBackend
from discord_agent_secretary.backends.linear import LinearBackend
from discord_agent_secretary.backends.multica import (
    MulticaBackend,
    MulticaCliError,
    MulticaCliTimeoutError,
    MulticaParseError,
)
from discord_agent_secretary.config import Settings

pytestmark = pytest.mark.unit


class TestProtocolConformance:
    def test_multica_backend_satisfies_protocol(self) -> None:
        backend = MulticaBackend(cli_path="multica")
        assert isinstance(backend, IssueBackend)

    def test_multica_backend_inherits_abc(self) -> None:
        backend = MulticaBackend(cli_path="multica")
        assert isinstance(backend, IssueBackendBase)

    def test_plain_object_does_not_satisfy_protocol(self) -> None:
        assert not isinstance(object(), IssueBackend)


class TestErrorHierarchy:
    """Concrete backend errors must inherit the abstract family."""

    def test_multica_timeout_inherits_backend_timeout(self) -> None:
        assert issubclass(MulticaCliTimeoutError, BackendTimeoutError)
        assert issubclass(MulticaCliTimeoutError, BackendError)

    def test_multica_cli_error_inherits_backend_call_error(self) -> None:
        assert issubclass(MulticaCliError, BackendCallError)
        assert issubclass(MulticaCliError, BackendError)

    def test_multica_parse_error_inherits_backend_parse_error(self) -> None:
        assert issubclass(MulticaParseError, BackendParseError)
        assert issubclass(MulticaParseError, BackendError)


class TestIssueRefImmutability:
    def test_frozen(self) -> None:
        ref = IssueRef(id="x")
        from dataclasses import FrozenInstanceError
        with pytest.raises(FrozenInstanceError):
            ref.id = "y"  # type: ignore[misc]

    def test_optional_fields_default_none(self) -> None:
        ref = IssueRef(id="x")
        assert ref.status is None
        assert ref.title is None


class TestMulticaCreateIssueArgs:
    async def test_create_issue_supports_parent_and_assignee(self, monkeypatch) -> None:
        backend = MulticaBackend(cli_path="multica")
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        async def fake_invoke(*args: str, **kwargs: object) -> bytes:
            calls.append((args, kwargs))
            return b'{"id":"child-uuid","identifier":"VEN-2","title":"child"}'

        monkeypatch.setattr(backend, "_invoke", fake_invoke)

        ref = await backend.create_issue(
            "child",
            description="body",
            priority="medium",
            assignee="GPT-5.5",
            parent="parent-uuid",
        )

        assert ref.id == "child-uuid"
        args, _kwargs = calls[0]
        assert args == (
            "issue",
            "create",
            "--title",
            "child",
            "--output",
            "json",
            "--description",
            "body",
            "--priority",
            "medium",
            "--assignee",
            "GPT-5.5",
            "--parent",
            "parent-uuid",
        )


class TestMakeBackend:
    def test_default_returns_multica(self, clean_settings) -> None:
        s = Settings(_env_file=None)
        backend = make_backend(s)
        assert isinstance(backend, MulticaBackend)

    def test_explicit_multica(self, monkeypatch, clean_settings) -> None:
        monkeypatch.setenv("BACKEND", "multica")
        s = Settings(_env_file=None)
        assert isinstance(make_backend(s), MulticaBackend)

    def test_github_backend_construction_fails_fast(
        self, monkeypatch, clean_settings
    ) -> None:
        monkeypatch.setenv("BACKEND", "github")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        s = Settings(_env_file=None)
        with pytest.raises(NotImplementedError):
            make_backend(s)

    def test_linear_backend_construction_fails_fast(
        self, monkeypatch, clean_settings
    ) -> None:
        monkeypatch.setenv("BACKEND", "linear")
        s = Settings(_env_file=None)
        with pytest.raises(NotImplementedError):
            make_backend(s)

    def test_jira_backend_construction_fails_fast(
        self, monkeypatch, clean_settings
    ) -> None:
        monkeypatch.setenv("BACKEND", "jira")
        s = Settings(_env_file=None)
        with pytest.raises(NotImplementedError):
            make_backend(s)


class TestStubBackendsFailFast:
    """Stubs must raise NotImplementedError at construction time so a
    misconfigured deployment exits immediately rather than at first use."""

    def test_github_init_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            GitHubBackend(token="t", repo="o/r")

    def test_linear_init_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            LinearBackend(api_key="k", team_id="t")

    def test_jira_init_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            JiraBackend(
                base_url="https://x.atlassian.net",
                email="e@x.com",
                api_token="t",
                project_key="ABC",
            )


class TestProtocolAbcAlignment:
    """Guard against drift between IssueBackend (Protocol) and IssueBackendBase."""

    def test_method_signatures_match(self) -> None:
        import inspect

        methods = ("create_issue", "get_issue", "assign_issue", "update_status", "add_comment")
        for name in methods:
            proto_sig = inspect.signature(getattr(IssueBackend, name))
            abc_sig = inspect.signature(getattr(IssueBackendBase, name))
            assert proto_sig == abc_sig, (
                f"{name} signature drift: Protocol={proto_sig} vs ABC={abc_sig}"
            )
