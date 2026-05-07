"""Unit tests for `discord_agent_secretary.health`.

Smoke-tests the stdlib-based healthcheck server end-to-end on a random
port and verifies routing for /livez, /readyz, and unknown paths.
"""
from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from discord_agent_secretary.health import start_healthcheck

pytestmark = pytest.mark.unit


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class TestHealthcheck:
    def test_disabled_when_port_zero(self) -> None:
        assert start_healthcheck(0, lambda: True) is None
        assert start_healthcheck(-1, lambda: True) is None

    def test_endpoints_route_correctly(self) -> None:
        # Bind to an ephemeral port via socketserver directly; our
        # `start_healthcheck` wrapper treats port=0 as "disabled".
        import socketserver
        import threading

        from discord_agent_secretary.health import (
            HealthcheckHandle,
            _make_handler,
        )

        handle = None
        try:
            ready = [True]
            handler = _make_handler(lambda: ready[0])
            server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever, daemon=True
            )
            thread.start()
            handle = HealthcheckHandle(server=server, thread=thread, port=port)

            base = f"http://127.0.0.1:{port}"
            assert _get(f"{base}/livez") == (200, "alive\n")
            assert _get(f"{base}/readyz") == (200, "ready\n")
            ready[0] = False
            assert _get(f"{base}/readyz") == (503, "not ready\n")
            status, body = _get(f"{base}/whatever")
            assert status == 404
            assert "not found" in body
        finally:
            if handle is not None:
                handle.shutdown()
