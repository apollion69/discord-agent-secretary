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


class TestMakeBackend:
    def test_default_returns_multica(self, clean_settings) -> None:
        s = Settings(_env_file=None)
        backend = make_backend(s)
        assert isinstance(backend, MulticaBackend)

    def test_explicit_multica(self, monkeypatch, clean_settings) -> None:
        monkeypatch.setenv("BACKEND", "multica")
        s = Settings(_env_file=None)
        assert isinstance(make_backend(s), MulticaBackend)

    def test_github_backend_constructs(self, monkeypatch, clean_settings) -> None:
        monkeypatch.setenv("BACKEND", "github")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        s = Settings(_env_file=None)
        backend = make_backend(s)
        assert isinstance(backend, GitHubBackend)

    def test_linear_backend_constructs(self, monkeypatch, clean_settings) -> None:
        monkeypatch.setenv("BACKEND", "linear")
        s = Settings(_env_file=None)
        backend = make_backend(s)
        assert isinstance(backend, LinearBackend)

    def test_jira_backend_constructs(self, monkeypatch, clean_settings) -> None:
        monkeypatch.setenv("BACKEND", "jira")
        s = Settings(_env_file=None)
        backend = make_backend(s)
        assert isinstance(backend, JiraBackend)


class TestStubBackendsNotImplemented:
    """Stubs must raise NotImplementedError on call, not on construction."""

    async def test_github_create_raises(self) -> None:
        backend = GitHubBackend(token="t", repo="o/r")
        with pytest.raises(NotImplementedError):
            await backend.create_issue("x")

    async def test_linear_create_raises(self) -> None:
        backend = LinearBackend(api_key="k", team_id="t")
        with pytest.raises(NotImplementedError):
            await backend.create_issue("x")

    async def test_jira_create_raises(self) -> None:
        backend = JiraBackend(
            base_url="https://x.atlassian.net",
            email="e@x.com",
            api_token="t",
            project_key="ABC",
        )
        with pytest.raises(NotImplementedError):
            await backend.create_issue("x")
