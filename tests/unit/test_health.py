"""Unit tests for `discord_agent_secretary.health`.

Drives the real `start_healthcheck` wrapper end-to-end on a random
ephemeral port. Verifies routing for /livez, /readyz, and unknown
paths, and exercises the daemon-thread crash logger by faking a
serve_forever exception.
"""
from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request

import pytest

from discord_agent_secretary.health import HealthcheckHandle, start_healthcheck

pytestmark = pytest.mark.unit


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class TestStartHealthcheck:
    def test_disabled_when_port_zero(self) -> None:
        assert start_healthcheck(0, lambda: True) is None
        assert start_healthcheck(-1, lambda: True) is None

    def test_endpoints_route_correctly(self) -> None:
        # Drive `start_healthcheck` itself — the previous version of this
        # test reconstructed the server inline, which silently bypassed
        # the wrapper under test.
        ready_flag = [True]
        handle = start_healthcheck(
            port=0, is_ready=lambda: ready_flag[0], bind="127.0.0.1"
        )
        # NOTE: port=0 short-circuits to None — we need a real port.
        # Allocate one explicitly so we can drive the wrapper.

        # Rebind via an OS-allocated port. start_healthcheck rejects 0,
        # so use the standard ephemeral-port-discovery dance: bind with
        # socketserver to grab a free port, close it, then call the wrapper.
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        # Some flake protection: if the port got taken between the close
        # above and the bind below, the test will fail with EADDRINUSE.
        # That's fine — pytest-rerunfailures or a single retry handles it.
        if handle is not None:
            handle.shutdown()  # the port=0 call above always returns None.

        handle = start_healthcheck(
            port=free_port,
            is_ready=lambda: ready_flag[0],
            bind="127.0.0.1",
        )
        try:
            assert handle is not None
            assert isinstance(handle, HealthcheckHandle)
            assert handle.port == free_port

            base = f"http://127.0.0.1:{free_port}"
            assert _get(f"{base}/livez") == (200, "alive\n")
            assert _get(f"{base}/readyz") == (200, "ready\n")
            ready_flag[0] = False
            assert _get(f"{base}/readyz") == (503, "not ready\n")
            status, body = _get(f"{base}/whatever")
            assert status == 404
            assert "not found" in body
        finally:
            if handle is not None:
                handle.shutdown()

    def test_handle_is_frozen(self) -> None:
        # The dataclass is frozen — attempting to rebind a field should
        # raise FrozenInstanceError. Documents the immutability contract.
        import socket
        import socketserver
        from dataclasses import FrozenInstanceError

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        from discord_agent_secretary.health import _make_handler

        handler = _make_handler(lambda: True)
        server = socketserver.ThreadingTCPServer(("127.0.0.1", free_port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        handle = HealthcheckHandle(server=server, thread=thread, port=free_port)
        try:
            with pytest.raises(FrozenInstanceError):
                handle.port = 9999  # type: ignore[misc]
        finally:
            handle.shutdown()

    def test_serve_loop_crash_is_logged(self, caplog) -> None:
        # The daemon thread wrapping `serve_forever` must log CRITICAL on
        # any exception so the operator notices instead of silently
        # discovering probes have stopped responding.
        import socket

        from discord_agent_secretary.health import start_healthcheck

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        # Patch ThreadingTCPServer.serve_forever to raise on first call.
        with caplog.at_level(logging.CRITICAL):
            from unittest.mock import patch

            real_thread = threading.Thread

            def _capture_thread(*args, **kwargs):
                t = real_thread(*args, **kwargs)
                t.start = lambda: real_thread.start(t)
                return t

            with patch(
                "discord_agent_secretary.health.socketserver.ThreadingTCPServer.serve_forever",
                side_effect=RuntimeError("boom in serve loop"),
            ):
                handle = start_healthcheck(
                    port=free_port,
                    is_ready=lambda: True,
                    bind="127.0.0.1",
                )
                assert handle is not None
                # Give the daemon thread a chance to hit the patched method.
                handle.thread.join(timeout=2.0)
                # serve_forever raised before it could set __is_shut_down, so
                # server.shutdown() would block forever — call server_close()
                # directly to release the socket without waiting on the event.
                handle.server.server_close()

        assert any(
            "healthcheck server crashed" in r.message
            for r in caplog.records
        )


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(url: str, body: bytes = b"{}") -> int:
    req = urllib.request.Request(url, data=body, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:  # noqa: S310
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


class TestLivenessAndRateLimit:
    def test_readyz_reports_worker_liveness(self) -> None:
        port = _free_port()
        handle = start_healthcheck(
            port=port, is_ready=lambda: True, bind="127.0.0.1",
            liveness=lambda: {"review-poll": "2026-05-29T00:00:00+00:00"},
        )
        try:
            status, body = _get(f"http://127.0.0.1:{port}/readyz")
            assert status == 200
            assert "worker=review-poll last_ok=2026-05-29T00:00:00+00:00" in body
        finally:
            assert handle is not None
            handle.shutdown()

    def test_webhook_rate_limited(self) -> None:
        port = _free_port()
        handle = start_healthcheck(
            port=port, is_ready=lambda: True, bind="127.0.0.1",
            webhook_callback=lambda b, s: None, rate_limit=1,
        )
        try:
            base = f"http://127.0.0.1:{port}/hooks/multica"
            assert _post(base) == 200
            assert _post(base) == 429  # second within the window is rejected
        finally:
            assert handle is not None
            handle.shutdown()
