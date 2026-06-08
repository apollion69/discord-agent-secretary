"""Components V2 task cards (opt-in rich UI for `/task` confirmations).

Discord's "Components V2" (discord.py >= 2.6) lets a bot render a structured
layout — a colour-accented container with a heading and a divider — instead of
a flat text line. This module builds the card for a freshly-created task. It is
enabled via `DISCORD_CARDS_ENABLED` and is otherwise completely inert: the
plain-text confirmation stays the default, so existing deployments are
unchanged.

Cards are pure builders — they construct a `discord.ui.LayoutView` and never
touch the network, so they unit-test by introspecting the view tree.
"""
from __future__ import annotations

from typing import Any

import discord

# Accent colour per priority — a quick visual signal in the channel.
_PRIORITY_ACCENT: dict[str, discord.Colour] = {
    "urgent": discord.Colour.red(),
    "high": discord.Colour.orange(),
    "medium": discord.Colour.blurple(),
    "low": discord.Colour.green(),
}
_DEFAULT_ACCENT: discord.Colour = discord.Colour.green()
_CARD_DESC_CAP = 1500


def priority_accent(priority: str | None) -> discord.Colour:
    """Map a priority to its container accent colour (default green)."""
    if priority is None:
        return _DEFAULT_ACCENT
    return _PRIORITY_ACCENT.get(priority.lower(), _DEFAULT_ACCENT)


class TaskCard(discord.ui.LayoutView):
    """A read-only Components V2 card summarising a created task.

    `heading` and `description` must already be markdown-escaped by the caller
    (the handler escapes user-supplied title/description before constructing
    the card).
    """

    def __init__(
        self,
        *,
        heading: str,
        ref_text: str,
        priority: str | None = None,
        description: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        container: discord.ui.Container[Any] = discord.ui.Container(
            accent_colour=priority_accent(priority)
        )
        container.add_item(discord.ui.TextDisplay(f"### ✅ {heading}"))
        line = ref_text
        if priority:
            line += f"  ·  **{discord.utils.escape_markdown(priority)}**"
        container.add_item(discord.ui.TextDisplay(line))
        if description and description.strip():
            container.add_item(discord.ui.Separator())
            desc = description.strip()
            if len(desc) > _CARD_DESC_CAP:
                desc = desc[:_CARD_DESC_CAP].rstrip() + "…"
            container.add_item(discord.ui.TextDisplay(desc))
        self.add_item(container)


def build_task_card(
    *,
    heading: str,
    ref_text: str,
    priority: str | None = None,
    description: str | None = None,
) -> TaskCard:
    """Build the Components V2 confirmation card for a created task."""
    return TaskCard(
        heading=heading,
        ref_text=ref_text,
        priority=priority,
        description=description,
    )
