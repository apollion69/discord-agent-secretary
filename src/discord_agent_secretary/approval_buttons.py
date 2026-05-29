"""Discord approval buttons for human sign-off on Multica tasks.

When an agent needs human approval it posts a comment containing the approval
marker and @mentions the member. The mention scanner then renders the message
with buttons instead of a plain ping:

  🟢 Согласовать  → issue status `done`
  🔴 На доработку → issue status `todo`
  🔵 Открыть в Multica (link)

A click is applied **as the clicking member** via act-as-member
(`MULTICA_ON_BEHALF_OF`), so Multica records who approved — not the bot.
Buttons are persistent `DynamicItem`s (custom_id carries the issue id), so they
keep working across bot restarts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import discord

from .config import get_settings

logger = logging.getLogger(__name__)

APPROVAL_MARKER = "[approval-request]"
_MARKER_RE = re.compile(r"\[approval-request\]", re.IGNORECASE)
_ACTION_STATUS = {"approve": "done", "rework": "todo"}


def is_approval_request(content: str) -> bool:
    return bool(_MARKER_RE.search(content or ""))


def strip_marker(content: str) -> str:
    return _MARKER_RE.sub("", content or "").strip()


async def apply_human_verdict(cli_path: str, issue_id: str, action: str, member_uuid: str) -> bool:
    """Apply the verdict as the member (act-as-member). Returns True on success."""
    status = _ACTION_STATUS.get(action)
    if status is None:
        return False
    env = {**os.environ, "MULTICA_ON_BEHALF_OF": member_uuid}
    proc = await asyncio.create_subprocess_exec(
        cli_path, "issue", "status", issue_id, status, "--output", "json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("approval verdict failed", extra={"issue_id": issue_id, "action": action, "detail": err.decode("utf-8", "replace")[:200]})
        return False
    return True


class ApprovalButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"appr:(?P<action>approve|rework):(?P<issue>[0-9a-fA-F-]{36})",
):
    """Persistent per-issue approve/rework button."""

    def __init__(self, action: str, issue_id: str) -> None:
        self.action = action
        self.issue_id = issue_id
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label="Согласовать" if approve else "На доработку",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                emoji="🟢" if approve else "🔴",
                custom_id=f"appr:{action}:{issue_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # noqa: ANN001
        return cls(match["action"], match["issue"])

    async def callback(self, interaction: discord.Interaction) -> None:  # noqa: PLR0911
        settings = get_settings()
        member_uuid = settings.discord_member_map.get(str(interaction.user.id))
        if not member_uuid:
            await interaction.response.send_message(
                "Эта кнопка доступна только участникам воркспейса.", ephemeral=True
            )
            return
        cli_path = settings.multica_cli_path or "multica"
        ok = await apply_human_verdict(cli_path, self.issue_id, self.action, member_uuid)
        if not ok:
            await interaction.response.send_message(
                "Не удалось применить — задача могла уже измениться. Открой её в Multica.",
                ephemeral=True,
            )
            return
        who = getattr(interaction.user, "display_name", None) or interaction.user.name
        verb = "✅ Согласовано" if self.action == "approve" else "🔁 Отправлено на доработку"
        base = interaction.message.content if interaction.message else ""
        await interaction.response.edit_message(content=f"{base}\n\n{verb} — {who}", view=None)
        logger.info("approval applied", extra={"issue_id": self.issue_id, "action": self.action, "by": str(interaction.user.id)})


def format_approval_request(discord_id: str, identifier: str, snippet: str) -> str:
    tail = f": «{snippet}»" if snippet else ""
    return (
        f"\U0001f6a6 <@{discord_id}>, требуется твоё согласование по {identifier}{tail}\n"
        "Нажми кнопку ниже — решение применится в Multica от твоего имени."
    )


def build_approval_view(issue_id: str, app_url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton("approve", issue_id))
    view.add_item(ApprovalButton("rework", issue_id))
    if app_url:
        view.add_item(
            discord.ui.Button(
                label="Открыть в Multica",
                style=discord.ButtonStyle.link,
                url=f"{app_url.rstrip('/')}/venchur/issues/{issue_id}",
                emoji="🔵",
            )
        )
    return view
