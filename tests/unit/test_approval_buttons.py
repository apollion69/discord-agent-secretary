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
        assert ok == "ok"
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
        assert ok == "ok"
        assert any("blocked" in a for a in seen)

    async def test_done_approve_sets_done_no_comment(self):
        seen = []

        async def fake_exec(*args, **kwargs):
            seen.append(args)
            return _FakeProc(0)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "done_approve", "m", "X")
        assert ok == "ok"
        assert any("done" in a for a in seen)
        assert not any("comment" in a for a in seen)

    async def test_unknown_action_rejected(self):
        assert await apply_human_verdict("multica", UUID, "nope", "m", "X") == "unknown_action"

    async def test_status_failure_returns_failed(self):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(2)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "done_approve", "m", "X")
        assert ok == "failed"

    async def test_done_rework_sets_todo_no_comment(self):
        seen = []

        async def fake_exec(*args, **kwargs):
            seen.append(args)
            return _FakeProc(0)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "done_rework", "m", "X")
        assert ok == "ok"
        assert any("todo" in a for a in seen)
        assert not any("comment" in a for a in seen)

    async def test_env_strips_discord_secrets(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "super-secret")
        monkeypatch.setenv("PATH", "/usr/bin")
        captured = {}

        async def fake_exec(*args, **kwargs):
            captured.update(kwargs.get("env", {}))
            return _FakeProc(0)

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            await apply_human_verdict("multica", UUID, "done_approve", "m-uuid", "X")
        assert "DISCORD_BOT_TOKEN" not in captured
        assert captured.get("MULTICA_ON_BEHALF_OF") == "m-uuid"

    async def test_start_comment_failure_still_succeeds(self):
        # status change ok, signal comment fails -> degraded but 'ok' (status moved)
        rcs = iter([0, 2])

        async def fake_exec(*args, **kwargs):
            return _FakeProc(next(rcs))

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "start_go", "m", "X")
        assert ok == "ok"

    async def test_timeout_returns_timeout(self):
        import asyncio as _aio

        class _HangProc:
            returncode = None

            async def communicate(self):
                await _aio.sleep(10)
                return b"", b""

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        async def fake_exec(*args, **kwargs):
            return _HangProc()

        with patch("discord_agent_secretary.approval_buttons.asyncio.create_subprocess_exec", side_effect=fake_exec):
            ok = await apply_human_verdict("multica", UUID, "done_approve", "m", "X", cli_timeout=0.01)
        assert ok == "timeout"


class _FakeResponse:
    def __init__(self):
        self.deferred = False
        self.ephemeral_msgs = []

    async def defer(self):
        self.deferred = True

    async def send_message(self, content, ephemeral=False):
        self.ephemeral_msgs.append((content, ephemeral))


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.name = "tester"
        self.display_name = "Tester"


class _FakeMessage:
    content = "request body"


class _FakeInteraction:
    def __init__(self, uid):
        self.user = _FakeUser(uid)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.message = _FakeMessage()
        self.edited = None

    async def edit_original_response(self, content, view=None):
        self.edited = content


class _Settings:
    def __init__(self, member_map):
        self.discord_member_map = member_map
        self.multica_cli_path = "multica"
        self.multica_cli_timeout = 30.0


class TestCallback:
    async def test_non_member_denied_no_defer(self):
        btn = ApprovalButton("start_go", UUID)
        inter = _FakeInteraction(uid="999")
        with patch("discord_agent_secretary.approval_buttons.get_settings", return_value=_Settings({})):
            await btn.callback(inter)
        assert inter.response.ephemeral_msgs  # got denial
        assert inter.response.deferred is False
        assert inter.edited is None

    async def test_member_success_edits_message(self):
        btn = ApprovalButton("start_go", UUID)
        inter = _FakeInteraction(uid="42")
        with (
            patch("discord_agent_secretary.approval_buttons.get_settings", return_value=_Settings({"42": "m-uuid"})),
            patch("discord_agent_secretary.approval_buttons.apply_human_verdict", return_value="ok") as verdict,
        ):
            await btn.callback(inter)
        assert inter.response.deferred is True
        assert verdict.await_args.args[2] == "start_go"
        assert "Старт разрешён" in inter.edited

    async def test_member_failure_followup_ephemeral(self):
        btn = ApprovalButton("done_approve", UUID)
        inter = _FakeInteraction(uid="42")
        with (
            patch("discord_agent_secretary.approval_buttons.get_settings", return_value=_Settings({"42": "m-uuid"})),
            patch("discord_agent_secretary.approval_buttons.apply_human_verdict", return_value="failed"),
        ):
            await btn.callback(inter)
        assert inter.response.deferred is True
        assert inter.followup.sent and inter.followup.sent[0][1] is True
        assert inter.edited is None

    async def test_member_timeout_shows_specific_message(self):
        btn = ApprovalButton("done_approve", UUID)
        inter = _FakeInteraction(uid="42")
        with (
            patch("discord_agent_secretary.approval_buttons.get_settings", return_value=_Settings({"42": "m-uuid"})),
            patch("discord_agent_secretary.approval_buttons.apply_human_verdict", return_value="timeout"),
        ):
            await btn.callback(inter)
        assert "вовремя" in inter.followup.sent[0][0]
        assert inter.edited is None
