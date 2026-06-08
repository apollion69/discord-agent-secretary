"""Unit tests for the scoped Discord MCP server (gateway + tool functions).

These exercise the dependency-light core — no `mcp` package and no network.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from discord_agent_secretary.mcp_server.gateway import (
    DiscordThreadGateway,
    ThreadInfo,
    ThreadMessage,
)
from discord_agent_secretary.mcp_server.server import (
    gateway_from_env,
    list_task_threads,
    post_in_thread,
    read_thread_messages,
)

pytestmark = pytest.mark.unit


class _Resp:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=MagicMock(), response=MagicMock(status_code=self.status_code)
            )

    def json(self) -> object:
        return self._payload


def _gateway(*, get: object = None, post: object = None) -> tuple[DiscordThreadGateway, MagicMock]:
    client = MagicMock()
    client.get = AsyncMock(return_value=get)
    client.post = AsyncMock(return_value=post)
    return DiscordThreadGateway("tok123", client=client), client


class TestListActiveThreads:
    async def test_parses_and_authenticates(self) -> None:
        payload = {"threads": [{"id": "10", "name": "VEN-1 fix", "parent_id": "99"}]}
        gw, client = _gateway(get=_Resp(payload))
        threads = await gw.list_active_threads("guild-1")
        assert threads == [ThreadInfo(id="10", name="VEN-1 fix", parent_id="99")]
        # Bot-token auth header present.
        assert client.get.call_args.kwargs["headers"]["Authorization"] == "Bot tok123"
        assert "guilds/guild-1/threads/active" in client.get.call_args.args[0]

    async def test_filters_by_parent(self) -> None:
        payload = {
            "threads": [
                {"id": "1", "name": "a", "parent_id": "99"},
                {"id": "2", "name": "b", "parent_id": "77"},
            ]
        }
        gw, _ = _gateway(get=_Resp(payload))
        threads = await gw.list_active_threads("g", parent_id="99")
        assert [t.id for t in threads] == ["1"]

    async def test_http_error_propagates(self) -> None:
        gw, _ = _gateway(get=_Resp({}, status=403))
        with pytest.raises(httpx.HTTPStatusError):
            await gw.list_active_threads("g")


class TestReadThread:
    async def test_parses_messages(self) -> None:
        payload = [
            {"id": "m1", "author": {"username": "alice"}, "content": "hi"},
            {"id": "m2", "author": {"username": "bob"}, "content": "yo"},
        ]
        gw, client = _gateway(get=_Resp(payload))
        msgs = await gw.read_thread("thr-1", limit=5)
        assert msgs == [
            ThreadMessage(id="m1", author="alice", content="hi"),
            ThreadMessage(id="m2", author="bob", content="yo"),
        ]
        assert client.get.call_args.kwargs["params"]["limit"] == 5

    async def test_limit_capped_at_100(self) -> None:
        gw, client = _gateway(get=_Resp([]))
        await gw.read_thread("t", limit=500)
        assert client.get.call_args.kwargs["params"]["limit"] == 100

    async def test_limit_floored_at_1(self) -> None:
        gw, client = _gateway(get=_Resp([]))
        await gw.read_thread("t", limit=0)
        assert client.get.call_args.kwargs["params"]["limit"] == 1


class TestPostInThread:
    async def test_suppresses_mentions(self) -> None:
        gw, client = _gateway(
            post=_Resp({"id": "m9", "author": {"username": "bot"}, "content": "done"})
        )
        msg = await gw.post_in_thread("thr-1", "done")
        assert msg == ThreadMessage(id="m9", author="bot", content="done")
        body = client.post.call_args.kwargs["json"]
        assert body["content"] == "done"
        assert body["allowed_mentions"] == {"parse": []}  # no mass-ping possible


class TestServerToolFunctions:
    async def test_list_task_threads_returns_dicts(self) -> None:
        gateway = MagicMock()
        gateway.list_active_threads = AsyncMock(
            return_value=[ThreadInfo(id="1", name="n", parent_id="2")]
        )
        out = await list_task_threads(gateway, "g", "2")
        assert out == [{"id": "1", "name": "n", "parent_id": "2"}]

    async def test_read_thread_messages_returns_dicts(self) -> None:
        gateway = MagicMock()
        gateway.read_thread = AsyncMock(
            return_value=[ThreadMessage(id="m", author="a", content="c")]
        )
        out = await read_thread_messages(gateway, "t", 10)
        assert out == [{"id": "m", "author": "a", "content": "c"}]

    async def test_post_in_thread_returns_dict(self) -> None:
        gateway = MagicMock()
        gateway.post_in_thread = AsyncMock(
            return_value=ThreadMessage(id="m", author="bot", content="hi")
        )
        out = await post_in_thread(gateway, "t", "hi")
        assert out == {"id": "m", "author": "bot", "content": "hi"}


class TestGatewayFromEnv:
    def test_missing_token_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("DISCORD_MCP_BOT_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="DISCORD_MCP_BOT_TOKEN"):
            gateway_from_env()

    def test_builds_with_token(self, monkeypatch) -> None:
        monkeypatch.setenv("DISCORD_MCP_BOT_TOKEN", "tok")
        gw = gateway_from_env()
        assert isinstance(gw, DiscordThreadGateway)
