"""Single ``on_message`` dispatcher.

discord.py allows only one ``on_message`` handler per client; this registers one
that awaits a list of independent per-message coroutines (the passive observer,
thread-reply sync, …) so multiple message features can coexist. Each handler is
awaited in turn and isolated — one raising does not stop the others.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence

import discord

logger = logging.getLogger(__name__)

MessageHandler = Callable[[discord.Message], Awaitable[None]]


def register_message_dispatch(
    client: discord.Client, handlers: Sequence[MessageHandler]
) -> None:
    """Register one ``on_message`` that fans out to every handler in `handlers`."""
    active = list(handlers)

    @client.event
    async def on_message(message: discord.Message) -> None:
        for handler in active:
            try:
                await handler(message)
            except Exception:  # noqa: BLE001 — one handler must not sink the others
                logger.exception("on_message handler raised")
