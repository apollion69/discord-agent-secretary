"""FastMCP entrypoint for the scoped Discord thread server.

The tool *logic* lives in plain async functions (`list_task_threads`,
`read_thread_messages`, `post_in_thread`) that take an injected gateway, so they
unit-test without the optional `mcp` dependency. `build_mcp` / `main` lazy-import
`mcp` and only wire those functions as tools — keeping `mcp` out of the core
install.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict
from typing import Any

from .gateway import DiscordThreadGateway

logger = logging.getLogger(__name__)

_TOKEN_ENV = "DISCORD_MCP_BOT_TOKEN"


def gateway_from_env() -> DiscordThreadGateway:
    """Build a gateway from `DISCORD_MCP_BOT_TOKEN`; raise if missing."""
    token = os.environ.get(_TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError(f"{_TOKEN_ENV} is required for the Discord MCP server")
    return DiscordThreadGateway(token)


async def list_task_threads(
    gateway: DiscordThreadGateway, guild_id: str, parent_id: str | None = None
) -> list[dict[str, Any]]:
    """List active threads in a guild (optionally one parent channel)."""
    threads = await gateway.list_active_threads(guild_id, parent_id=parent_id)
    return [asdict(t) for t in threads]


async def read_thread_messages(
    gateway: DiscordThreadGateway, thread_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Read the most recent messages in a thread."""
    messages = await gateway.read_thread(thread_id, limit=limit)
    return [asdict(m) for m in messages]


async def post_in_thread(
    gateway: DiscordThreadGateway, thread_id: str, content: str
) -> dict[str, Any]:
    """Post a message in an existing thread (mentions suppressed)."""
    return asdict(await gateway.post_in_thread(thread_id, content))


def build_mcp(gateway: DiscordThreadGateway) -> Any:  # pragma: no cover - needs `mcp`
    """Wire the three tools onto a FastMCP server. Requires the `[mcp]` extra."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "the 'mcp' extra is required: pip install 'discord-agent-secretary[mcp]'"
        ) from exc

    mcp = FastMCP("discord-task-threads")

    @mcp.tool()
    async def list_threads(guild_id: str, parent_id: str | None = None) -> list[dict[str, Any]]:
        """List active task threads in a guild (optionally one parent channel)."""
        return await list_task_threads(gateway, guild_id, parent_id)

    @mcp.tool()
    async def read_thread(thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Read recent messages from a task thread."""
        return await read_thread_messages(gateway, thread_id, limit)

    @mcp.tool()
    async def post_message(thread_id: str, content: str) -> dict[str, Any]:
        """Post a message into an existing task thread (no mass-pings)."""
        return await post_in_thread(gateway, thread_id, content)

    return mcp


def main() -> int:  # pragma: no cover - process entrypoint
    logging.basicConfig(level=logging.INFO)
    gateway = gateway_from_env()
    build_mcp(gateway).run()
    return 0
