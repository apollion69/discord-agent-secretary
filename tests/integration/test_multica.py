"""Integration tests for the Multica CLI backend.

The CLI child process is mocked at `asyncio.create_subprocess_exec` so tests
stay hermetic. Covers happy paths for create/get/assign, timeout, non-zero
exit, malformed JSON, missing field, empty output, and header-line preamble
stripping.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from discord_agent_secretary.backends import CircuitState, IssueRef
from discord_agent_secretary.backends.multica import (
    MulticaBackend,
    MulticaCliError,
    MulticaCliTimeoutError,
    MulticaParseError,
)

pytestmark = pytest.mark.integration


class _FakeStream:
    """StreamReader stand-in that supports the `read(N)` shape we use."""

    def __init__(self, data: bytes, *, hang: bool = False) -> None:
        self._data = data
        self._hang = hang
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if self._hang:
            import asyncio as _a

            await _a.sleep(10)
        if n is None or n < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


class _FakeProc:
    """Stand-in for `asyncio.subprocess.Process`."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self.stdout = _FakeStream(stdout, hang=hang)
        self.stderr = _FakeStream(stderr)
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        # Kept for backward compatibility with any older test that touched
        # this. New tests use the read()-based path through proc.stdout /
        # proc.stderr, matching MulticaBackend's actual implementation.
        if self._hang:
            import asyncio as _a

            await _a.sleep(10)
        return await self.stdout.read(-1), await self.stderr.read(-1)

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0

    def kill(self) -> None:
        self.killed = True


def _patch_spawn(proc: _FakeProc, captured: list[tuple[Any, ...]] | None = None):
    """Return a context manager that replaces the async process spawner."""

    async def _fake(*args: Any, **_kwargs: Any) -> _FakeProc:
        if captured is not None:
            captured.append(args)
        return proc

    return patch(
        "discord_agent_secretary.backends.multica.asyncio.create_subprocess_exec",
        side_effect=_fake,
    )


class TestCreateIssue:
    async def test_success_returns_issue_ref(self):
        proc = _FakeProc(stdout=b'{"id":"VEN-42","status":"todo","title":"Fix"}')
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            ref = await backend.create_issue("Fix", priority="high")
        assert ref == IssueRef(id="VEN-42", status="todo", title="Fix")

    async def test_forwards_optional_flags(self):
        proc = _FakeProc(stdout=b'{"id":"VEN-1"}')
        captured: list[tuple[Any, ...]] = []
        with _patch_spawn(proc, captured):
            backend = MulticaBackend(cli_path="/usr/bin/multica")
            await backend.create_issue(
                "T", description="D", priority="high", assignee="alice"
            )
        argv = list(captured[0])
        assert argv[0] == "/usr/bin/multica"
        assert "--description" in argv and "D" in argv
        assert "--priority" in argv and "high" in argv
        assert "--assignee" in argv and "alice" in argv

    async def test_cli_timeout_raises_typed_error(self):
        proc = _FakeProc(hang=True)
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica", timeout=0.05)
            with pytest.raises(MulticaCliTimeoutError):
                await backend.create_issue("X")
        assert proc.killed is True

    async def test_non_zero_exit_raises_cli_error(self):
        proc = _FakeProc(stderr=b"boom", returncode=2)
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            with pytest.raises(MulticaCliError) as exc:
                await backend.create_issue("X")
        assert exc.value.exit_code == 2
        assert "boom" in exc.value.stderr

    async def test_malformed_json_raises_parse_error(self):
        proc = _FakeProc(stdout=b"not-json-at-all")
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            with pytest.raises(MulticaParseError):
                await backend.create_issue("X")

    async def test_missing_id_field_raises_parse_error(self):
        proc = _FakeProc(stdout=b'{"status":"todo"}')
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            with pytest.raises(MulticaParseError):
                await backend.create_issue("X")

    async def test_empty_output_raises_parse_error(self):
        proc = _FakeProc(stdout=b"")
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            with pytest.raises(MulticaParseError):
                await backend.create_issue("X")


class TestGetIssue:
    async def test_valid_id_returns_ref(self):
        proc = _FakeProc(stdout=b'{"id":"VEN-7","status":"in_progress"}')
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            ref = await backend.get_issue("VEN-7")
        assert ref.id == "VEN-7"
        assert ref.status == "in_progress"

    async def test_invalid_id_surfaces_cli_error(self):
        proc = _FakeProc(stderr=b"not found", returncode=1)
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            with pytest.raises(MulticaCliError):
                await backend.get_issue("does-not-exist")


class TestAssignIssue:
    async def test_assign_success(self):
        proc = _FakeProc(stdout=b'{"id":"VEN-5","status":"todo"}')
        captured: list[tuple[Any, ...]] = []
        with _patch_spawn(proc, captured):
            backend = MulticaBackend(cli_path="multica")
            ref = await backend.assign_issue("VEN-5", to="bob")
        assert ref.id == "VEN-5"
        argv = list(captured[0])
        assert "assign" in argv and "--to" in argv and "bob" in argv


class TestAddComment:
    async def test_invokes_comment_add_cli(self):
        proc = _FakeProc(stdout=b"ok")
        captured: list[tuple[Any, ...]] = []
        with _patch_spawn(proc, captured):
            backend = MulticaBackend(cli_path="multica")
            await backend.add_comment("VEN-3", "looks good")
        argv = list(captured[0])
        assert argv[1:4] == ["issue", "comment", "add"]
        assert "VEN-3" in argv
        assert "--content" in argv and "looks good" in argv

    async def test_on_behalf_of_sets_env(self):
        proc = _FakeProc(stdout=b"ok")
        seen: dict[str, Any] = {}

        async def _fake(*_args: Any, **kwargs: Any) -> _FakeProc:
            seen["env"] = kwargs.get("env")
            return proc

        with patch(
            "discord_agent_secretary.backends.multica.asyncio.create_subprocess_exec",
            side_effect=_fake,
        ):
            backend = MulticaBackend(cli_path="multica")
            await backend.add_comment("VEN-3", "hi", on_behalf_of="member-uuid-7")
        assert seen["env"] is not None
        assert seen["env"]["MULTICA_ON_BEHALF_OF"] == "member-uuid-7"

    async def test_cli_error_propagates(self):
        proc = _FakeProc(stderr=b"nope", returncode=1)
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            with pytest.raises(MulticaCliError):
                await backend.add_comment("VEN-3", "x")


class TestPreambleStripping:
    async def test_header_line_before_json_is_dropped(self):
        proc = _FakeProc(
            stdout=b'Showing 1 of 1 comments.\n{"id":"VEN-9","status":"done"}'
        )
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            ref = await backend.get_issue("VEN-9")
        assert ref == IssueRef(id="VEN-9", status="done", title=None)

    async def test_array_prefix_after_preamble_is_kept(self):
        # The strip-preamble guard recognises both `{` and `[` as valid
        # JSON starts. This guards against a mutation that tightens to
        # `{` only (which would silently drop array-shaped responses).
        from discord_agent_secretary.backends.multica import _strip_preamble

        body = "Listing 3 items.\n[{\"id\":\"A-1\"}]"
        assert _strip_preamble(body) == "[{\"id\":\"A-1\"}]"


class TestReapEdgeCases:
    async def test_returncode_none_raises_cli_error(self):
        # If the process exits but `returncode` stays None (e.g. wait()
        # resolves but the OS doesn't set a status), the backend raises
        # rather than returning garbage.
        proc = _FakeProc(stdout=b'{"id":"VEN-3"}', returncode=None)
        # _FakeProc.wait() returns 0 when returncode is None, but
        # self.returncode stays None — that's the contract we're testing.
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            with pytest.raises(MulticaCliError) as exc:
                await backend.get_issue("VEN-3")
        assert "without an exit status" in exc.value.stderr

    async def test_reap_timeout_is_logged_as_warning(self, caplog):
        # If the child is still alive after SIGKILL + _REAP_TIMEOUT, the
        # backend logs SIGKILL timeout warning rather than hanging forever.
        import logging
        from unittest.mock import patch

        from discord_agent_secretary.backends.multica import _reap

        proc = MagicMock()
        proc.pid = 999

        async def _slow_wait():
            import asyncio as _a
            await _a.sleep(5)

        proc.wait = _slow_wait

        with caplog.at_level(logging.WARNING):
            with patch(
                "discord_agent_secretary.backends.multica._REAP_TIMEOUT",
                new=0.01,
            ):
                await _reap(proc)

        assert any(
            "still running after SIGKILL" in r.message
            for r in caplog.records
        )

    async def test_empty_string_id_raises_parse_error(self):
        # `_to_issue_ref` must reject an id that is present but empty.
        # Guards against a mutation that weakens `not issue_id` to `issue_id is None`.
        proc = _FakeProc(stdout=b'{"id":""}')
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            with pytest.raises(MulticaParseError):
                await backend.create_issue("X")

    async def test_non_dict_json_raises_parse_error(self):
        # The output may be valid JSON but not an object — must raise,
        # not fail with an AttributeError in _to_issue_ref.
        proc = _FakeProc(stdout=b'"just a string"')
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica")
            with pytest.raises(MulticaParseError, match="expected JSON object"):
                await backend.create_issue("X")


class TestMulticaBackendCircuitProperty:
    def test_circuit_property_exposes_breaker(self):
        # Exposed for observability (Prometheus metrics, health endpoint).
        backend = MulticaBackend(cli_path="multica")
        assert backend.circuit.state == CircuitState.CLOSED

    def test_stderr_log_cap_truncates_long_stderr(self):
        from discord_agent_secretary.backends.multica import _STDERR_LOG_CAP, MulticaCliError

        long_err = "x" * (_STDERR_LOG_CAP + 100)
        err = MulticaCliError(exit_code=1, stderr=long_err)
        # The stored full stderr is intact.
        assert err.stderr == long_err
        # The __str__ (passed to super().__init__) is truncated.
        assert len(str(err)) < len(long_err) + 50
        assert "…" in str(err)


class TestOutputCap:
    async def test_overflow_kills_and_raises(self):
        # 100 bytes of stdout against a 32-byte limit -> overflow.
        proc = _FakeProc(stdout=b"x" * 100)
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica", output_byte_limit=32)
            with pytest.raises(MulticaCliError) as exc:
                await backend.get_issue("VEN-1")
        assert "exceeded" in exc.value.stderr
        assert proc.killed is True


class TestRetryAndCircuitBreaker:
    async def test_get_issue_retries_on_timeout(self):
        # First spawn hangs -> timeout; second returns clean output.
        # We need two distinct procs because the test infra is per-call.
        good = _FakeProc(stdout=b'{"id":"VEN-2"}')
        bad = _FakeProc(hang=True)
        procs = iter([bad, good])

        async def _fake(*args, **kwargs):
            return next(procs)

        from unittest.mock import patch

        with patch(
            "discord_agent_secretary.backends.multica.asyncio.create_subprocess_exec",
            side_effect=_fake,
        ):
            backend = MulticaBackend(cli_path="multica", timeout=0.05)
            ref = await backend.get_issue("VEN-2")
        assert ref.id == "VEN-2"

    async def test_create_issue_does_not_retry(self):
        # create_issue is non-idempotent — a single timeout must surface.
        proc = _FakeProc(hang=True)
        with _patch_spawn(proc):
            backend = MulticaBackend(cli_path="multica", timeout=0.05)
            with pytest.raises(MulticaCliTimeoutError):
                await backend.create_issue("X")

    async def test_circuit_opens_after_threshold(self):
        # Threshold = 1: a single failure opens the circuit; subsequent
        # calls fast-fail with CircuitOpenError without spawning.
        from discord_agent_secretary.backends import CircuitOpenError

        proc = _FakeProc(hang=True)
        spawn_count = 0

        async def _fake(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            return proc

        from unittest.mock import patch

        with patch(
            "discord_agent_secretary.backends.multica.asyncio.create_subprocess_exec",
            side_effect=_fake,
        ):
            backend = MulticaBackend(
                cli_path="multica",
                timeout=0.05,
                circuit_failure_threshold=1,
                circuit_reset_timeout=60.0,
            )
            with pytest.raises(MulticaCliTimeoutError):
                await backend.create_issue("X")
            spawn_count_after_open = spawn_count
            with pytest.raises(CircuitOpenError):
                await backend.create_issue("Y")
        # No new subprocess spawned after the circuit opened.
        assert spawn_count == spawn_count_after_open


class TestActAsMemberEnv:
    """create_issue propagates on_behalf_of to the CLI subprocess environment."""

    @staticmethod
    def _capture_env(proc: _FakeProc, sink: dict[str, Any]):
        async def _fake(*_args: Any, **kwargs: Any) -> _FakeProc:
            sink["env"] = kwargs.get("env")
            return proc

        return patch(
            "discord_agent_secretary.backends.multica.asyncio.create_subprocess_exec",
            side_effect=_fake,
        )

    async def test_on_behalf_of_sets_env(self):
        proc = _FakeProc(stdout=b'{"id":"VEN-1"}')
        sink: dict[str, Any] = {}
        with self._capture_env(proc, sink):
            backend = MulticaBackend(cli_path="multica")
            await backend.create_issue("T", on_behalf_of="member-uuid-7")
        assert sink["env"] is not None
        assert sink["env"]["MULTICA_ON_BEHALF_OF"] == "member-uuid-7"
        # Parent environment is preserved, not replaced.
        assert "PATH" in sink["env"]

    async def test_without_on_behalf_of_env_is_none(self):
        proc = _FakeProc(stdout=b'{"id":"VEN-2"}')
        sink: dict[str, Any] = {}
        with self._capture_env(proc, sink):
            backend = MulticaBackend(cli_path="multica")
            await backend.create_issue("T")
        # No override → env stays None so the child inherits the parent env.
        assert sink["env"] is None


class TestParseErrorTripsBreaker:
    """A malformed CLI response counts toward the circuit breaker."""

    async def test_parse_error_opens_circuit(self):
        proc = _FakeProc(stdout=b"not-json-at-all")
        backend = MulticaBackend(cli_path="multica", circuit_failure_threshold=1)
        with _patch_spawn(proc):
            with pytest.raises(MulticaParseError):
                await backend.create_issue("X")
        assert backend.circuit.state == CircuitState.OPEN
