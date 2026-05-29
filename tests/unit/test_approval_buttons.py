"""Unit tests for approval-button helpers."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from discord_agent_secretary.approval_buttons import (
    ApprovalButton,
    apply_human_verdict,
    build_approval_view,
    format_approval_request,
    is_approval_request,
    strip_marker,
)

pytestmark = pytest.mark.unit

UUID = "b9b2070e-e5f8-450b-aadc-d3268970c9a5"


class TestMarker:
    def test_detects_marker(self):
        assert is_approval_request("please sign off [approval-request] thanks")
        assert is_approval_request("[APPROVAL-REQUEST]")

    def test_no_marker(self):
        assert not is_approval_request("just a normal @mention")

    def test_strip_marker(self):
        assert strip_marker("approve this [approval-request] now") == "approve this  now".strip()


class TestRender:
    def test_approval_message(self):
        msg = format_approval_request("123", "VEN-9", "deploy the thing")
        assert "<@123>" in msg
        assert "VEN-9" in msg
        assert "deploy the thing" in msg

    def test_view_has_three_buttons_with_app_url(self):
        view = build_approval_view(UUID, "http://m.local:3000")
        assert len(view.children) == 3
        dyn = [c for c in view.children if isinstance(c, ApprovalButton)]
        assert {b.action for b in dyn} == {"approve", "rework"}
        assert all(b.issue_id == UUID for b in dyn)

    def test_view_two_buttons_without_app_url(self):
        view = build_approval_view(UUID, "")
        assert len(view.children) == 2


class _FakeProc:
    def __init__(self, rc):
        self.returncode = rc

    async def communicate(self):
        return b"{}", b"" if self.returncode == 0 else b"boom"


class TestApplyVerdict:
    async def test_approve_maps_to_done(self):
        captured = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            return _FakeProc(0)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "approve", "member-uuid")
        assert ok is True
        assert "done" in captured["args"]
        assert captured["env"]["MULTICA_ON_BEHALF_OF"] == "member-uuid"

    async def test_rework_maps_to_todo(self):
        captured = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            return _FakeProc(0)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "rework", "m")
        assert ok is True
        assert "todo" in captured["args"]

    async def test_unknown_action_rejected(self):
        assert await apply_human_verdict("multica", UUID, "nope", "m") is False

    async def test_cli_failure_returns_false(self):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(2)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "approve", "m")
        assert ok is False
