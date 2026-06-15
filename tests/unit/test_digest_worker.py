"""Unit tests for the daily autopilot digest worker."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import pytest

from discord_agent_secretary.digest_worker import (
    DigestWorker,
    format_digest,
)

pytestmark = pytest.mark.unit

APP = "http://m.local:3000"


def _iss(ident, **kw):
    return {"id": ident.lower(), "identifier": ident, **kw}


class TestFormatDigest:
    def test_none_when_empty(self):
        assert format_digest([], [], APP) is None

    def test_counts_and_links(self):
        msg = format_digest([_iss("VEN-1")], [_iss("VEN-2"), _iss("VEN-3")], APP)
        assert "В review: 1" in msg
        assert "Выполнено за 24ч: 2" in msg
        assert "[VEN-1](<http://m.local:3000/venchur/issues/ven-1>)" in msg
        assert "отключены" in msg

    def test_truncates_long_bucket(self):
        many = [_iss(f"VEN-{n}") for n in range(12)]
        msg = format_digest(many, [], APP)
        assert "В review: 12" in msg
        assert "+4 ещё" in msg


class TestSchedule:
    def _worker(self, tmp: Path, now: datetime, hour=9):
        return DigestWorker(
            cli_path="multica", channel_id=1, app_url=APP, tz="Europe/Moscow",
            digest_hour=hour, state_path=tmp / "d.json", now_fn=lambda: now,
        )

    def test_due_after_hour_when_not_posted_today(self, tmp_path):
        now = datetime(2026, 5, 29, 9, 30, tzinfo=ZoneInfo("Europe/Moscow"))
        w = self._worker(tmp_path, now)
        assert w._due(now, None) is True

    def test_not_due_before_hour(self, tmp_path):
        now = datetime(2026, 5, 29, 8, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        w = self._worker(tmp_path, now)
        assert w._due(now, None) is False

    def test_not_due_if_already_posted_today(self, tmp_path):
        now = datetime(2026, 5, 29, 9, 30, tzinfo=ZoneInfo("Europe/Moscow"))
        w = self._worker(tmp_path, now)
        assert w._due(now, "2026-05-29") is False

    def test_not_due_on_saturday_after_hour(self, tmp_path):
        now = datetime(2026, 6, 13, 9, 30, tzinfo=ZoneInfo("Europe/Moscow"))
        w = self._worker(tmp_path, now)
        assert w._due(now, "2026-06-12") is False

    def test_not_due_on_sunday_after_hour(self, tmp_path):
        now = datetime(2026, 6, 14, 9, 30, tzinfo=ZoneInfo("Europe/Moscow"))
        w = self._worker(tmp_path, now)
        assert w._due(now, "2026-06-12") is False

    def test_monday_due_after_friday_digest(self, tmp_path):
        now = datetime(2026, 6, 15, 9, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        w = self._worker(tmp_path, now)
        assert w._due(now, "2026-06-12") is True

    def test_sleep_capped_at_one_hour(self, tmp_path):
        now = datetime(2026, 5, 29, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        w = self._worker(tmp_path, now)
        assert w._sleep_seconds(now) == 3600.0


class _FakeChannel(discord.abc.Messageable):
    def __init__(self):
        self.sent = []

    async def _get_channel(self):
        return self

    async def send(self, message):
        self.sent.append(message)


class _FakeClient:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, _cid):
        return self._channel


class TestPost:
    def _worker(self, tmp):
        return DigestWorker(
            cli_path="multica", channel_id=1, app_url=APP, tz="Europe/Moscow",
            digest_hour=9, state_path=tmp / "d.json",
        )

    async def test_post_returns_true_on_send(self, tmp_path):
        ch = _FakeChannel()
        ok = await self._worker(tmp_path)._post(_FakeClient(ch), "hi")
        assert ok is True and ch.sent == ["hi"]

    async def test_post_returns_false_when_not_messageable(self, tmp_path):
        ok = await self._worker(tmp_path)._post(_FakeClient(object()), "hi")
        assert ok is False


class TestBuild:
    async def test_build_filters_autopilot_and_window(self, tmp_path, monkeypatch):
        recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        lists = {
            "in_review": [
                _iss("VEN-A", origin_type="autopilot"),
                _iss("VEN-B", origin_type=None),  # human, excluded
            ],
            "done": [
                _iss("VEN-C", origin_type="autopilot", updated_at=recent),
                _iss("VEN-D", origin_type="autopilot", updated_at=old),  # too old
            ],
        }
        w = DigestWorker(
            cli_path="multica", channel_id=1, app_url=APP, tz="Europe/Moscow",
            digest_hour=9, state_path=tmp_path / "d.json",
        )

        async def fake_list(status):
            return lists[status]

        monkeypatch.setattr(w, "_list", fake_list)
        msg = await w.build()
        assert "В review: 1" in msg  # only VEN-A
        assert "Выполнено за 24ч: 1" in msg  # only VEN-C
        assert "VEN-A" in msg and "VEN-C" in msg
        assert "VEN-B" not in msg and "VEN-D" not in msg

    async def test_monday_build_uses_previous_digest_window_and_names_weekend(
        self, tmp_path, monkeypatch
    ):
        tz = ZoneInfo("Europe/Moscow")
        now = datetime(2026, 6, 15, 9, 0, tzinfo=tz)
        friday_after_digest = datetime(2026, 6, 12, 10, 0, tzinfo=tz).astimezone(UTC).isoformat()
        friday_before_digest = datetime(2026, 6, 12, 8, 0, tzinfo=tz).astimezone(UTC).isoformat()
        lists = {
            "in_review": [_iss("VEN-A", origin_type="autopilot")],
            "done": [
                _iss("VEN-B", origin_type="autopilot", updated_at=friday_after_digest),
                _iss("VEN-C", origin_type="autopilot", updated_at=friday_before_digest),
            ],
        }
        w = DigestWorker(
            cli_path="multica",
            channel_id=1,
            app_url=APP,
            tz="Europe/Moscow",
            digest_hour=9,
            state_path=tmp_path / "d.json",
            now_fn=lambda: now,
        )

        async def fake_list(status):
            return lists[status]

        monkeypatch.setattr(w, "_list", fake_list)
        msg = await w.build(last_date="2026-06-12")
        assert "Сводка авто-задач за выходные (13.06.2026-14.06.2026)" in msg
        assert "Выполнено с прошлой сводки: 1" in msg
        assert "VEN-B" in msg
        assert "VEN-C" not in msg
