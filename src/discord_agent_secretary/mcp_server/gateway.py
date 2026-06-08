"""Scoped Discord REST gateway for the MCP server.

Three operations only — list active threads, read a thread, post in a thread —
each a single Discord REST call with a ``Bot`` token. The httpx client is
injectable so the gateway unit-tests without network or the `mcp` package.

Safety: every post sets ``allowed_mentions: {parse: []}`` so an agent can never
trigger a mass-ping; ``read_thread`` caps ``limit`` to Discord's 100.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API = "https://discord.com/api/v10"
_NO_PINGS: dict[str, Any] = {"parse": []}
_DEFAULT_TIMEOUT = 10.0
_MAX_READ = 100


@dataclass(frozen=True)
class ThreadInfo:
    id: str
    name: str
    parent_id: str | None = None


@dataclass(frozen=True)
class ThreadMessage:
    id: str
    author: str
    content: str


class DiscordThreadGateway:
    """Async wrapper over the three thread operations the MCP server exposes."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = _API,
    ) -> None:
        self._token = token
        self._base = base_url.rstrip("/")
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        return self._client

    async def list_active_threads(
        self, guild_id: str, *, parent_id: str | None = None
    ) -> list[ThreadInfo]:
        """Active threads in a guild, optionally filtered to one parent channel."""
        client = await self._get_client()
        resp = await client.get(
            f"{self._base}/guilds/{guild_id}/threads/active", headers=self._headers()
        )
        resp.raise_for_status()
        data = resp.json()
        threads = data.get("threads", []) if isinstance(data, dict) else []
        out: list[ThreadInfo] = []
        for t in threads:
            if not isinstance(t, dict):
                continue
            pid = t.get("parent_id")
            if parent_id is not None and str(pid) != str(parent_id):
                continue
            out.append(
                ThreadInfo(
                    id=str(t.get("id")),
                    name=str(t.get("name") or ""),
                    parent_id=str(pid) if pid is not None else None,
                )
            )
        return out

    async def read_thread(self, thread_id: str, *, limit: int = 20) -> list[ThreadMessage]:
        """The most recent messages in a thread (newest first, capped at 100)."""
        client = await self._get_client()
        capped = max(1, min(int(limit), _MAX_READ))
        resp = await client.get(
            f"{self._base}/channels/{thread_id}/messages",
            params={"limit": capped},
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        out: list[ThreadMessage] = []
        for m in data if isinstance(data, list) else []:
            if not isinstance(m, dict):
                continue
            author_raw = m.get("author")
            author = author_raw if isinstance(author_raw, dict) else {}
            out.append(
                ThreadMessage(
                    id=str(m.get("id")),
                    author=str(author.get("username") or "?"),
                    content=str(m.get("content") or ""),
                )
            )
        return out

    async def post_in_thread(self, thread_id: str, content: str) -> ThreadMessage:
        """Post a message in an existing thread (mentions suppressed)."""
        client = await self._get_client()
        resp = await client.post(
            f"{self._base}/channels/{thread_id}/messages",
            headers=self._headers(),
            json={"content": content, "allowed_mentions": _NO_PINGS},
        )
        resp.raise_for_status()
        raw = resp.json() if callable(getattr(resp, "json", None)) else {}
        m = raw if isinstance(raw, dict) else {}
        author_raw = m.get("author")
        author = author_raw if isinstance(author_raw, dict) else {}
        return ThreadMessage(
            id=str(m.get("id", "")),
            author=str(author.get("username") or "bot"),
            content=str(m.get("content") or content),
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
