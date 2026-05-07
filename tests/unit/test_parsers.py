"""Unit tests for discord_agent_secretary.parsers.

Golden cases come from tests/fixtures/multilingual_cases.yaml (loaded via the
`multilingual_cases` fixture in conftest.py). Additional edge cases are
defined inline for finer-grained coverage.
"""
from __future__ import annotations

import dataclasses

import pytest

from discord_agent_secretary.parsers import ParsedTask, parse_task


class TestGoldenCasesYAML:
    """Parameterized over tests/fixtures/multilingual_cases.yaml."""

    def test_golden(self, multilingual_cases, frozen_today):
        assert multilingual_cases, "YAML fixture should load at least one case"
        failures: list[str] = []
        for case in multilingual_cases:
            result = parse_task(case["input"], today=frozen_today)
            expected = case["expected"]
            for key, want in expected.items():
                got = getattr(result, key)
                if got != want:
                    failures.append(
                        f"[{case['id']}] {key}: expected {want!r}, got {got!r}"
                    )
        assert not failures, "\n".join(failures)


class TestPriority:
    def test_p1_maps_to_high(self):
        assert parse_task("/task [P1] X").priority == "high"

    def test_p2_maps_to_medium(self):
        assert parse_task("/task [P2] X").priority == "medium"

    def test_p3_maps_to_low(self):
        assert parse_task("/task [P3] X").priority == "low"

    def test_lowercase_p1_accepted(self):
        assert parse_task("/task [p1] X").priority == "high"

    def test_ru_urgent_maps_to_high(self):
        assert parse_task("/task X [срочно]").priority == "high"

    def test_ru_normal_maps_to_medium(self):
        assert parse_task("/task X [обычно]").priority == "medium"

    def test_bracket_whitespace_tolerated(self):
        assert parse_task("/task [ P1 ] X").priority == "high"

    def test_no_bracket_means_no_priority(self):
        assert parse_task("/task Plain title").priority is None


class TestDate:
    def test_iso_date_extracted(self, frozen_today):
        r = parse_task("/task Do 2026-04-25", today=frozen_today)
        assert r.deadline == "2026-04-25"

    def test_iso_date_consumes_leading_by(self, frozen_today):
        r = parse_task("/task Do by 2026-04-25", today=frozen_today)
        assert r.deadline == "2026-04-25"
        assert r.title == "Do"

    def test_ru_dotted_full_year(self, frozen_today):
        r = parse_task("/task Do 25.04.2026", today=frozen_today)
        assert r.deadline == "2026-04-25"

    def test_ru_dotted_short_uses_current_year(self, frozen_today):
        r = parse_task("/task Do 25.04", today=frozen_today)
        assert r.deadline == "2026-04-25"

    def test_relative_en_in_N_days(self, frozen_today):  # noqa: N802
        r = parse_task("/task Do in 3 days", today=frozen_today)
        assert r.deadline == "2026-04-24"

    def test_relative_ru_za_N_dnya(self, frozen_today):  # noqa: N802
        r = parse_task("/task Do за 2 дня", today=frozen_today)
        assert r.deadline == "2026-04-23"

    def test_ru_dotted_short_does_not_swallow_full_date(self, frozen_today):
        r = parse_task("/task X 25.04.2026", today=frozen_today)
        assert r.deadline == "2026-04-25"

    def test_ru_short_date_rolls_forward_when_past(self):
        from datetime import date

        # today is May 1; "25.04" must mean next April, not last April.
        r = parse_task("/task до 25.04", today=date(2026, 5, 1))
        assert r.deadline == "2027-04-25"

    def test_ru_short_invalid_date_kept_in_title(self):
        from datetime import date

        r = parse_task("/task до 31.02", today=date(2026, 4, 21))
        assert r.deadline is None
        assert "31.02" in r.title

    def test_tz_aware_today_default(self):
        # Smoke: passing a valid IANA zone should not raise and should pick
        # up a date — we don't assert the value because it depends on now().
        r = parse_task("/task ping in 1 days", tz="Europe/Moscow")
        assert r.deadline is not None


class TestAssignee:
    def test_mention_handle_extracted(self):
        assert parse_task("/task Ping @alice").assignee == "alice"

    def test_numeric_mention_extracted(self):
        r = parse_task("/task Ping <@123456789>")
        assert r.assignee_id == 123456789
        assert r.assignee is None

    def test_numeric_mention_with_bang_extracted(self):
        assert parse_task("/task Ping <@!987654321>").assignee_id == 987654321

    def test_email_like_at_not_captured(self):
        r = parse_task("/task Mail user@example")
        assert r.assignee is None
        assert "user@example" in r.title

    def test_first_handle_wins(self):
        r = parse_task("/task Ping @alice @bob")
        assert r.assignee == "alice"


class TestCombined:
    def test_all_four_fields(self, frozen_today):
        r = parse_task("/task [P1] Fix 2026-04-25 @bob", today=frozen_today)
        assert r == ParsedTask(
            title="Fix", priority="high", deadline="2026-04-25", assignee="bob"
        )

    def test_unicode_title_preserved(self):
        r = parse_task("/task Привет мир")
        assert r.title == "Привет мир"

    def test_japanese_title_preserved(self):
        r = parse_task("/task 日本語です")
        assert r.title == "日本語です"

    def test_ru_word_do_stays_in_title(self, frozen_today):
        r = parse_task("/task [P1] Кириллица до 25.04", today=frozen_today)
        assert r.title == "Кириллица до"
        assert r.deadline == "2026-04-25"

    def test_whitespace_collapsed(self):
        r = parse_task("/task   Multiple    spaces")
        assert r.title == "Multiple spaces"


class TestImmutability:
    def test_parsed_task_is_frozen(self):
        r = parse_task("/task X")
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.title = "changed"  # type: ignore[misc]
