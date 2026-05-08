"""HTTP healthcheck endpoints for `/livez` and `/readyz`.

A tiny stdlib `http.server` runs in a daemon thread alongside the asyncio
event loop. We deliberately avoid `aiohttp` / `starlette` here — bot deploys
in Kubernetes and systemd already get value from a 30-line probe surface,
and adding a web framework just for two endpoints is not worth the cost.

Wire-up: `main.py` calls `start_healthcheck(port, is_ready)`; the returned
handle exposes `.shutdown()` so a SIGTERM path can close the listener
cleanly.
"""
from __future__ import annotations

import http.server
import logging
import socketserver
import threading
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HealthcheckHandle:
    """Return value from `start_healthcheck` — owns the server + thread.

    Frozen because the fields are wired up at construction and never
    rebound; `shutdown()` operates on the contained objects only.
    """

    server: socketserver.TCPServer
    thread: threading.Thread
    port: int

    def shutdown(self) -> None:
        # `TCPServer.shutdown` is a blocking call that signals the polling
        # loop in `serve_forever`; safe to call from the main asyncio thread.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)


def _make_handler(is_ready: Callable[[], bool]) -> type[http.server.BaseHTTPRequestHandler]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib API
            if self.path == "/livez":
                self._respond(200, "alive\n")
            elif self.path == "/readyz":
                ok = bool(is_ready())
                self._respond(
                    200 if ok else 503,
                    "ready\n" if ok else "not ready\n",
                )
            else:
                self._respond(404, "not found\n")

        def log_message(self, format: str, *args: object) -> None:
            # Default logger spits to stderr in a non-structured shape; we
            # silence it and emit one structured line per request through
            # the project logger instead.
            logger.info(
                "healthcheck request",
                extra={
                    "method": self.command,
                    "path": self.path,
                    "client": self.client_address[0],
                },
            )

        def _respond(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

    return _Handler


def start_healthcheck(
    port: int,
    is_ready: Callable[[], bool],
    *,
    bind: str = "0.0.0.0",
) -> HealthcheckHandle | None:
    """Start the HTTP probe server in a daemon thread.

    Returns `None` when `port <= 0` so callers can use a plain truthiness
    check at shutdown time without a separate disabled-flag.
    """
    if port <= 0:
        return None

    handler = _make_handler(is_ready)
    server = socketserver.ThreadingTCPServer((bind, port), handler)
    server.daemon_threads = True
    actual_port = server.server_address[1]
    def _serve() -> None:
        try:
            server.serve_forever()
        except Exception as exc:  # noqa: BLE001
            logger.critical(
                "healthcheck server crashed — probes will fail",
                extra={"detail": str(exc)},
            )

    thread = threading.Thread(
        target=_serve,
        name=f"healthcheck-{actual_port}",
        daemon=True,
    )
    thread.start()
    logger.info(
        "healthcheck listening", extra={"bind": bind, "port": actual_port}
    )
    return HealthcheckHandle(server=server, thread=thread, port=actual_port)
