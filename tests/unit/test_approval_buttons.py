"""Unit tests for approval-button helpers (start + completion gates)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from discord_agent_secretary.approval_buttons import (
    ApprovalButton,
    apply_human_verdict,
    build_approval_view,
    format_approval_request,
    parse_approval_type,
    strip_marker,
)

pytestmark = pytest.mark.unit

UUID = "b9b2070e-e5f8-450b-aadc-d3268970c9a5"


class TestMarker:
    def test_bare_marker_is_done(self):
        assert parse_approval_type("sign off [approval-request] now") == "done"

    def test_explicit_start(self):
        assert parse_approval_type("[approval-request:start] go?") == "start"

    def test_explicit_done(self):
        assert parse_approval_type("[APPROVAL-REQUEST:DONE]") == "done"

    def test_no_marker(self):
        assert parse_approval_type("just a @mention") is None

    def test_strip(self):
        assert strip_marker("begin [approval-request:start] please") == "begin  please".strip()


class TestView:
    def test_start_view_buttons(self):
        view = build_approval_view("start", UUID, "http://m.local:3000")
        assert len(view.children) == 3
        dyn = [c for c in view.children if isinstance(c, ApprovalButton)]
        assert {b.action for b in dyn} == {"start_go", "start_decline"}

    def test_done_view_buttons(self):
        view = build_approval_view("done", UUID, "http://m.local:3000")
        dyn = [c for c in view.children if isinstance(c, ApprovalButton)]
        assert {b.action for b in dyn} == {"done_approve", "done_rework"}

    def test_view_no_app_url_two_buttons(self):
        assert len(build_approval_view("start", UUID, "").children) == 2

    def test_message_text_by_type(self):
        assert "начала работы" in format_approval_request("start", "1", "VEN-9", "x")
        assert "завершения" in format_approval_request("done", "1", "VEN-9", "x")


class _FakeProc:
    def __init__(self, rc):
        self.returncode = rc

    async def communicate(self):
        return b"{}", b""


class TestApplyVerdict:
    async def test_start_go_sets_in_progress_and_comments(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs.get("env", {}).get("MULTICA_ON_BEHALF_OF")))
            return _FakeProc(0)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "start_go", "m-uuid", "Egor")
        assert ok is True
        # two CLI calls: status in_progress, then the [start-approved] comment
        assert any("in_progress" in a for a, _ in calls)
        assert any("comment" in a and any("[start-approved]" in str(x) for x in a) for a, _ in calls)
        assert all(env == "m-uuid" for _, env in calls)

    async def test_start_decline_sets_blocked(self):
        seen = []

        async def fake_exec(*args, **kwargs):
            seen.append(args)
            return _FakeProc(0)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "start_decline", "m", "X")
        assert ok is True
        assert any("blocked" in a for a in seen)

    async def test_done_approve_sets_done_no_comment(self):
        seen = []

        async def fake_exec(*args, **kwargs):
            seen.append(args)
            return _FakeProc(0)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "done_approve", "m", "X")
        assert ok is True
        assert any("done" in a for a in seen)
        assert not any("comment" in a for a in seen)

    async def test_unknown_action_rejected(self):
        assert await apply_human_verdict("multica", UUID, "nope", "m", "X") is False

    async def test_status_failure_returns_false(self):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(2)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "done_approve", "m", "X")
        assert ok is False
