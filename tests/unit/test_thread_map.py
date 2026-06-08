"""Unit tests for discord_agent_secretary.thread_map."""
from __future__ import annotations

import pytest

from discord_agent_secretary.thread_map import ThreadMap

pytestmark = pytest.mark.unit


class TestThreadMap:
    def test_set_and_lookup_both_ways(self, tmp_path) -> None:
        m = ThreadMap(tmp_path / "tm.json")
        m.set("issue-1", 1001)
        assert m.thread_for_issue("issue-1") == 1001
        assert m.issue_for_thread(1001) == "issue-1"
        assert len(m) == 1

    def test_unknown_returns_none(self, tmp_path) -> None:
        m = ThreadMap(tmp_path / "tm.json")
        assert m.thread_for_issue("nope") is None
        assert m.issue_for_thread(999) is None

    def test_persists_across_reload(self, tmp_path) -> None:
        path = tmp_path / "tm.json"
        ThreadMap(path).set("issue-2", 2002)
        reloaded = ThreadMap(path)
        assert reloaded.thread_for_issue("issue-2") == 2002
        assert reloaded.issue_for_thread(2002) == "issue-2"

    def test_reassign_thread_drops_old_reverse(self, tmp_path) -> None:
        m = ThreadMap(tmp_path / "tm.json")
        m.set("issue-3", 3003)
        m.set("issue-3", 3004)  # moved threads
        assert m.thread_for_issue("issue-3") == 3004
        assert m.issue_for_thread(3003) is None
        assert m.issue_for_thread(3004) == "issue-3"

    def test_cap_evicts_oldest(self, tmp_path) -> None:
        m = ThreadMap(tmp_path / "tm.json", max_entries=2)
        m.set("a", 1)
        m.set("b", 2)
        m.set("c", 3)  # evicts "a"
        assert len(m) == 2
        assert m.thread_for_issue("a") is None
        assert m.issue_for_thread(1) is None
        assert m.thread_for_issue("c") == 3

    def test_corrupt_file_starts_empty(self, tmp_path) -> None:
        path = tmp_path / "tm.json"
        path.write_text("{not json", encoding="utf-8")
        m = ThreadMap(path)
        assert len(m) == 0
        m.set("x", 7)  # still usable
        assert m.thread_for_issue("x") == 7

    def test_ignores_malformed_entries_on_load(self, tmp_path) -> None:
        path = tmp_path / "tm.json"
        path.write_text(
            '{"issue_to_thread": {"ok": 5, "bad": "not-int"}}', encoding="utf-8"
        )
        m = ThreadMap(path)
        assert m.thread_for_issue("ok") == 5
        assert m.thread_for_issue("bad") is None
