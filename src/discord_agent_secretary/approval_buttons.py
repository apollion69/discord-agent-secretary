"""Discord approval buttons for human-in-the-loop task gating.

Two approval types, selected by the comment marker an agent posts:

  [approval-request:start] (or :done)  — gate on STARTING work
      🟢 Разрешить начать → status in_progress + comment "[start-approved] by X"
      🔴 Отклонить        → status blocked      + comment "[start-declined] by X"

  [approval-request]  /  [approval-request:done] — gate on COMPLETION
      🟢 Согласовать  → status done
      🔴 На доработку → status todo

Both render a 🔵 Открыть в Multica link. A click is applied **as the clicking
member** via act-as-member (MULTICA_ON_BEHALF_OF), so Multica records who
decided — not the bot. The start-approval also drops a machine-readable comment
the waiting agent polls to know it may proceed (or must abandon). Buttons are
persistent DynamicItems (custom_id carries the action + issue id), so they keep
working across bot restarts; only mapped workspace members may click.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import discord

from .config import get_settings

logger = logging.getLogger(__name__)

# [approval-request] / [approval-request:start] / [approval-request:done]
_MARKER_RE = re.compile(r"\[approval-request(?::(start|done))?\]", re.IGNORECASE)

# action -> (target status, optional comment marker the agent can poll, button label, style, outcome text)
_ACTIONS: dict[str, dict[str, object]] = {
    "start_go": {
        "status": "in_progress", "comment": "[start-approved]",
        "label": "Разрешить начать", "emoji": "🟢", "ok": True,
        "outcome": "🟢 Старт разрешён",
    },
    "start_decline": {
        "status": "blocked", "comment": "[start-declined]",
        "label": "Отклонить", "emoji": "🔴", "ok": False,
        "outcome": "🔴 Старт отклонён",
    },
    "done_approve": {
        "status": "done", "comment": None,
        "label": "Согласовать", "emoji": "🟢", "ok": True,
        "outcome": "✅ Согласовано",
    },
    "done_rework": {
        "status": "todo", "comment": None,
        "label": "На доработку", "emoji": "🔴", "ok": False,
        "outcome": "🔁 На доработку",
    },
}
_TYPE_ACTIONS = {"start": ("start_go", "start_decline"), "done": ("done_approve", "done_rework")}


def parse_approval_type(content: str) -> str | None:
    """Return 'start', 'done', or None. A bare [approval-request] means 'done'."""
    m = _MARKER_RE.search(content or "")
    if not m:
        return None
    return (m.group(1) or "done").lower()


def strip_marker(content: str) -> str:
    return _MARKER_RE.sub("", content or "").strip()


async def apply_human_verdict(
    cli_path: str, issue_id: str, action: str, member_uuid: str, member_name: str = ""
) -> bool:
    """Apply the verdict as the member (act-as-member). Returns True on success."""
    spec = _ACTIONS.get(action)
    if spec is None:
        return False
    env = {**os.environ, "MULTICA_ON_BEHALF_OF": member_uuid}

    async def _cli(*args: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            cli_path, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("approval CLI failed", extra={"issue_id": issue_id, "action": action, "detail": err.decode("utf-8", "replace")[:200]})
        return proc.returncode or 0

    if await _cli("issue", "status", issue_id, str(spec["status"]), "--output", "json") != 0:
        return False
    marker = spec["comment"]
    if marker:
        # Machine-readable signal the waiting agent polls; attributed to the human.
        await _cli("issue", "comment", "add", issue_id, "--content", f"{marker} by {member_name}".strip())
    return True


class ApprovalButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"appr:(?P<action>start_go|start_decline|done_approve|done_rework):(?P<issue>[0-9a-fA-F-]{36})",
):
    """Persistent per-issue approval button (start or completion)."""

    def __init__(self, action: str, issue_id: str) -> None:
        self.action = action
        self.issue_id = issue_id
        spec = _ACTIONS[action]
        super().__init__(
            discord.ui.Button(
                label=str(spec["label"]),
                style=discord.ButtonStyle.success if spec["ok"] else discord.ButtonStyle.danger,
                emoji=str(spec["emoji"]),
                custom_id=f"appr:{action}:{issue_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # noqa: ANN001
        return cls(match["action"], match["issue"])

    async def callback(self, interaction: discord.Interaction) -> None:
        settings = get_settings()
        member_uuid = settings.discord_member_map.get(str(interaction.user.id))
        if not member_uuid:
            await interaction.response.send_message(
                "Эта кнопка доступна только участникам воркспейса.", ephemeral=True
            )
            return
        who = getattr(interaction.user, "display_name", None) or interaction.user.name
        cli_path = settings.multica_cli_path or "multica"
        ok = await apply_human_verdict(cli_path, self.issue_id, self.action, member_uuid, who)
        if not ok:
            await interaction.response.send_message(
                "Не удалось применить — задача могла уже измениться. Открой её в Multica.",
                ephemeral=True,
            )
            return
        outcome = str(_ACTIONS[self.action]["outcome"])
        base = interaction.message.content if interaction.message else ""
        await interaction.response.edit_message(content=f"{base}\n\n{outcome} — {who}", view=None)
        logger.info("approval applied", extra={"issue_id": self.issue_id, "action": self.action, "by": str(interaction.user.id)})


def build_approval_view(approval_type: str, issue_id: str, app_url: str) -> discord.ui.View:
    go_action, no_action = _TYPE_ACTIONS.get(approval_type, _TYPE_ACTIONS["done"])
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton(go_action, issue_id))
    view.add_item(ApprovalButton(no_action, issue_id))
    if app_url:
        view.add_item(
            discord.ui.Button(
                label="Открыть в Multica", style=discord.ButtonStyle.link,
                url=f"{app_url.rstrip('/')}/venchur/issues/{issue_id}", emoji="🔵",
            )
        )
    return view


def format_approval_request(approval_type: str, discord_id: str, identifier: str, snippet: str) -> str:
    what = "начала работы" if approval_type == "start" else "завершения"
    tail = f": «{snippet}»" if snippet else ""
    return (
        f"\U0001f6a6 <@{discord_id}>, требуется твоё согласование {what} по {identifier}{tail}\n"
        "Нажми кнопку ниже — решение применится в Multica от твоего имени."
    )
