"""Minimal-permission Discord MCP server (optional ``[mcp]`` extra).

Gives an LLM agent a *scoped* Discord surface — list task threads, read a thread,
post in an existing thread — and nothing else. No Administrator, no privileged
gateway intents, no channel management. The bot token is read from the
``DISCORD_MCP_BOT_TOKEN`` environment variable (never hard-coded), addressing the
token-mismanagement (OWASP MCP01) risk the deep-research flagged for the
broadly-scoped third-party Discord MCP servers.

Public surface:
  * `DiscordThreadGateway` — thin async Discord REST wrapper (uses httpx).
  * `ThreadInfo`, `ThreadMessage` — frozen result dataclasses.
  * `server.main` — the FastMCP entrypoint (lazy-imports the `mcp` package).
"""
from __future__ import annotations

from .gateway import DiscordThreadGateway, ThreadInfo, ThreadMessage

__all__ = ["DiscordThreadGateway", "ThreadInfo", "ThreadMessage"]
