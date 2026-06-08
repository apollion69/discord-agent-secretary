"""Unit tests for discord_agent_secretary.cards (Components V2)."""
from __future__ import annotations

import discord
import pytest

from discord_agent_secretary.cards import (
    TaskCard,
    build_task_card,
    priority_accent,
)

pytestmark = pytest.mark.unit


def _texts(view: discord.ui.LayoutView) -> list[str]:
    """Collect every TextDisplay content in the view tree."""
    out: list[str] = []
    for top in view.children:
        children = getattr(top, "children", [])
        for item in children:
            content = getattr(item, "content", None)
            if isinstance(content, str):
                out.append(content)
    return out


class TestPriorityAccent:
    def test_known_priorities(self) -> None:
        assert priority_accent("urgent") == discord.Colour.red()
        assert priority_accent("high") == discord.Colour.orange()
        assert priority_accent("medium") == discord.Colour.blurple()
        assert priority_accent("low") == discord.Colour.green()

    def test_none_and_unknown_default_green(self) -> None:
        assert priority_accent(None) == discord.Colour.green()
        assert priority_accent("weird") == discord.Colour.green()

    def test_case_insensitive(self) -> None:
        assert priority_accent("HIGH") == discord.Colour.orange()


class TestBuildTaskCard:
    def test_is_layout_view_with_container(self) -> None:
        card = build_task_card(heading="Fix login", ref_text="`VEN-9`")
        assert isinstance(card, TaskCard)
        assert isinstance(card, discord.ui.LayoutView)
        assert len(card.children) == 1
        assert isinstance(card.children[0], discord.ui.Container)

    def test_heading_and_ref_present(self) -> None:
        card = build_task_card(heading="Fix login", ref_text="[VEN-9](<http://m/issues/VEN-9>)")
        joined = "\n".join(_texts(card))
        assert "Fix login" in joined
        assert "VEN-9" in joined

    def test_priority_rendered_and_accents_container(self) -> None:
        card = build_task_card(heading="t", ref_text="`X-1`", priority="high")
        assert card.children[0].accent_colour == discord.Colour.orange()
        assert "high" in "\n".join(_texts(card))

    def test_description_adds_separator(self) -> None:
        card = build_task_card(
            heading="t", ref_text="`X-1`", description="some details here"
        )
        kinds = [type(c).__name__ for c in card.children[0].children]
        assert "Separator" in kinds
        assert "some details here" in "\n".join(_texts(card))

    def test_no_description_no_separator(self) -> None:
        card = build_task_card(heading="t", ref_text="`X-1`")
        kinds = [type(c).__name__ for c in card.children[0].children]
        assert "Separator" not in kinds

    def test_long_description_truncated(self) -> None:
        card = build_task_card(heading="t", ref_text="`X-1`", description="x" * 5000)
        joined = "\n".join(_texts(card))
        assert "…" in joined
        assert card.content_length() < 4000  # under Discord's V2 char cap

    def test_blank_description_ignored(self) -> None:
        card = build_task_card(heading="t", ref_text="`X-1`", description="   ")
        kinds = [type(c).__name__ for c in card.children[0].children]
        assert "Separator" not in kinds
