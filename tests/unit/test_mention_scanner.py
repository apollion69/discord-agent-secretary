"""Unit tests for the mention scanner's pure helpers."""
from __future__ import annotations

import asyncio

import pytest

from discord_agent_secretary.mention_scanner import (
    _load_state,
    _save_state,
    build_member_discord_map,
    extract_member_mentions,
    format_mention_ping,
    readable_snippet,
)

pytestmark = pytest.mark.unit

MID = "2b5f6c2c-b3c8-4f2a-9c1d-acc51149c300"  # r.gilmanov member id


class TestExtractMemberMentions:
    def test_extracts_member_id(self):
        c = f"hey [@r.gilmanov](mention://member/{MID}) please check"
        assert extract_member_mentions(c) == {MID}

    def test_ignores_agent_and_issue_mentions(self):
        c = "[@bot](mention://agent/abc) see [VEN-1](mention://issue/xyz)"
        assert extract_member_mentions(c) == set()

    def test_multiple_members(self):
        c = f"[@a](mention://member/{MID}) and [@b](mention://member/aaaa-bbbb)"
        assert extract_member_mentions(c) == {MID, "aaaa-bbbb"}

    def test_empty(self):
        assert extract_member_mentions("") == set()


class TestBuildMemberDiscordMap:
    def test_joins_via_user_id(self):
        members = [
            {"id": "m1", "user_id": "u1"},
            {"id": "m2", "user_id": "u2"},
            {"id": "m3", "user_id": "u3"},  # not in discord map
        ]
        dm = {"d1": "u1", "d2": "u2"}  # discord_id -> user_id
        assert build_member_discord_map(members, dm) == {
            "m1": "d1",
            "u1": "d1",
            "m2": "d2",
            "u2": "d2",
        }

    def test_indexes_multica_user_id_mentions(self):
        members = [{"id": "member-row-1", "user_id": "user-r-gilmanov"}]
        dm = {"discord-r-gilmanov": "user-r-gilmanov"}

        assert build_member_discord_map(members, dm) == {
            "member-row-1": "discord-r-gilmanov",
            "user-r-gilmanov": "discord-r-gilmanov",
        }


class TestRendering:
    def test_snippet_strips_mention_links(self):
        c = f"[@r.gilmanov](mention://member/{MID}) glance at the rollout"
        assert readable_snippet(c) == "@r.gilmanov glance at the rollout"

    def test_ping_format_with_link(self):
        msg = format_mention_ping("123", "VEN-9", "uuid-9", "http://m.local:3000", "check this")
        assert "<@123>" in msg
        assert "[VEN-9](<http://m.local:3000/venchur/issues/uuid-9>)" in msg
        assert "«check this»" in msg

    def test_ping_format_no_app_url(self):
        msg = format_mention_ping("123", "VEN-9", "uuid-9", "", "")
        assert "<@123>" in msg and "VEN-9" in msg
        assert "<http" not in msg

    def test_snippet_escapes_markdown(self):
        # comment markdown must not bleed into the Discord message
        out = readable_snippet("**bold** and `code` and _em_")
        assert "**" not in out
        assert r"\*\*bold\*\*" in out


class TestStateEviction:
    def test_seen_eviction_keeps_most_recent_by_insertion(self, tmp_path):
        path = tmp_path / "seen.json"
        # 5001 ids inserted in order id-00000 .. id-05000; cap is 5000.
        seen = {f"id-{n:05d}": None for n in range(5001)}
        _save_state(path, seen, {"i": "t"})
        reloaded, issue_seen = _load_state(path)
        assert len(reloaded) == 5000
        # the OLDEST (id-00000) is dropped, the NEWEST (id-05000) is kept
        assert "id-00000" not in reloaded
        assert "id-05000" in reloaded
        assert issue_seen == {"i": "t"}


class TestMemberMapTTL:
    def _worker(self, tmp_path, ttl):
        from pathlib import Path

        from discord_agent_secretary.mention_scanner import MentionScanWorker
        return MentionScanWorker(
            cli_path="multica", channel_id=1, app_url="", statuses=["todo"],
            discord_member_map={"42": "u1"}, state_path=Path(tmp_path / "s.json"),
            poll_interval=30.0, member_map_ttl=ttl,
        )

    async def test_cached_within_ttl(self, tmp_path):
        w = self._worker(tmp_path, ttl=300.0)
        calls = {"n": 0}

        async def fake_cli(*args):
            calls["n"] += 1
            return {"members": [{"id": "m1", "user_id": "u1"}]}

        w._cli_json = fake_cli
        await w._member_discord_map()
        await w._member_discord_map()
        assert calls["n"] == 1  # second call served from cache

    async def test_refetch_when_ttl_zero(self, tmp_path):
        w = self._worker(tmp_path, ttl=0.0)
        calls = {"n": 0}

        async def fake_cli(*args):
            calls["n"] += 1
            return {"members": [{"id": "m1", "user_id": "u1"}]}

        w._cli_json = fake_cli
        await w._member_discord_map()
        await w._member_discord_map()
        assert calls["n"] == 2  # ttl=0 → always refetch


class TestMentionScanWorker:
    async def test_scans_comments_even_when_issue_updated_at_is_unchanged(
        self, tmp_path, monkeypatch
    ):
        from pathlib import Path

        from discord_agent_secretary import mention_scanner
        from discord_agent_secretary.mention_scanner import MentionScanWorker

        issue_id = "86396a42-a275-426e-a2fc-e342adfdc3a6"
        issue_updated_at = "2026-06-04T08:18:37Z"
        comment_id = "13fc4708-866a-4a2f-bbcc-f4fccb2fdeed"
        state_path = Path(tmp_path / "mention-seen.json")
        _save_state(state_path, {}, {issue_id: issue_updated_at})
        sent: list[str] = []

        worker = MentionScanWorker(
            cli_path="multica",
            channel_id=1,
            app_url="https://multica.example",
            statuses=["in_review"],
            discord_member_map={"42": "u1"},
            state_path=state_path,
            poll_interval=30.0,
        )

        async def fake_cli(*args):
            if args[:3] == ("workspace", "member", "list"):
                return {"members": [{"id": MID, "user_id": "u1"}]}
            if args[:2] == ("issue", "list"):
                return {
                    "issues": [
                        {
                            "id": issue_id,
                            "identifier": "VEN-1641",
                            "status": "in_review",
                            "updated_at": issue_updated_at,
                        }
                    ]
                }
            if args[:3] == ("issue", "comment", "list"):
                return {
                    "comments": [
                        {
                            "id": comment_id,
                            "created_at": "2026-06-04T14:13:31Z",
                            "updated_at": "2026-06-04T14:13:31Z",
                            "content": f"[@r.gilmanov](mention://member/{MID}) почитай тред",
                        }
                    ]
                }
            raise AssertionError(args)

        async def fake_send(_client, message, view=None):
            sent.append(message)

        async def stop_after_cycle(_sleep_s):
            raise asyncio.CancelledError

        worker._cli_json = fake_cli
        worker._send = fake_send
        monkeypatch.setattr(mention_scanner.asyncio, "sleep", stop_after_cycle)

        with pytest.raises(asyncio.CancelledError):
            await worker.run(client=object())

        assert len(sent) == 1
        assert "<@42>" in sent[0]
        seen, _ = _load_state(state_path)
        assert comment_id in seen
