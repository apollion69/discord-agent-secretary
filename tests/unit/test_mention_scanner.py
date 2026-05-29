"""Unit tests for the mention scanner's pure helpers."""
from __future__ import annotations

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
        assert build_member_discord_map(members, dm) == {"m1": "d1", "m2": "d2"}


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
