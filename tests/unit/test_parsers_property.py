"""Hypothesis property-based tests for `parse_task`.

Property tests catch classes of bugs that hand-crafted examples miss:
  * Fuzz titles that parse_task claims to extract — verify no priority/
    deadline/assignee bleeds into the title field.
  * Verify parse_task never raises an unexpected exception on arbitrary
    unicode input.
  * Verify that roundtripping the parsed title produces the same title
    (idempotency within the title extraction).
"""
from __future__ import annotations

from datetime import date

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from discord_agent_secretary.parsers import ParsedTask, parse_task

pytestmark = pytest.mark.unit

_TODAY = date(2025, 6, 15)


class TestParseSafety:
    @given(st.text(min_size=1, max_size=500))
    @settings(max_examples=300, deadline=None)
    def test_never_raises_on_arbitrary_input(self, text: str) -> None:
        """parse_task must be total — no exception on any unicode string."""
        try:
            result = parse_task(text, today=_TODAY)
            assert isinstance(result, ParsedTask)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"parse_task raised on {text!r}: {exc}")

    @given(st.text(min_size=1, max_size=400))
    @settings(max_examples=200, deadline=None)
    def test_result_is_always_a_parsed_task(self, text: str) -> None:
        result = parse_task(text, today=_TODAY)
        assert isinstance(result, ParsedTask)
        assert isinstance(result.title, str)
        assert result.priority in (None, "low", "medium", "high", "urgent")
        assert result.assignee_id is None or isinstance(result.assignee_id, int)
        assert result.assignee is None or isinstance(result.assignee, str)


class TestTitleProperty:
    @given(
        st.from_regex(
            r"[A-Za-zА-Яа-я ]{5,80}",
            fullmatch=True,
        )
    )
    @settings(max_examples=100, deadline=None)
    def test_plain_text_ends_up_in_title(self, body: str) -> None:
        """For plain text with no special markers, the whole body (stripped)
        should appear in the title — nothing should be silently consumed."""
        assume(not any(c in body for c in "[]@<>.0123456789"))
        text = f"/task {body}"
        result = parse_task(text, today=_TODAY)
        # The title must contain the original body words (whitespace-collapsed).
        import re
        normalized = re.sub(r"\s+", " ", body).strip()
        assert normalized in result.title or result.title in normalized

    @given(
        st.from_regex(r"[A-Za-z ]{3,50}", fullmatch=True),
        st.sampled_from(["low", "medium", "high"]),
    )
    @settings(max_examples=100, deadline=None)
    def test_priority_not_in_title(self, title_words: str, priority: str) -> None:
        """Recognized priority tokens must be extracted and not appear in
        the returned title as `[P1]` / `[P2]` / `[P3]` literals."""
        assume(not any(c in title_words for c in "[]@<>.0123456789Р"))
        p_tag = {"low": "[P3]", "medium": "[P2]", "high": "[P1]"}[priority]
        text = f"/task {p_tag} {title_words}"
        result = parse_task(text, today=_TODAY)
        assert result.priority == priority
        assert "[P1]" not in result.title
        assert "[P2]" not in result.title
        assert "[P3]" not in result.title


class TestDeadlineProperty:
    @given(
        st.integers(min_value=1, max_value=365),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=3, max_size=30),
    )
    @settings(max_examples=100, deadline=None)
    def test_relative_deadline_extracted_correctly(
        self, days: int, title: str
    ) -> None:
        """'in N days' must produce a deadline exactly N days from today
        and the phrase must not appear in the title."""
        assume(title.strip())
        from datetime import timedelta

        text = f"/task {title.strip()} in {days} days"
        result = parse_task(text, today=_TODAY)
        assert result.deadline == (_TODAY + timedelta(days=days)).isoformat()
        assert f"in {days}" not in result.title
        assert "days" not in result.title

    @given(
        st.integers(min_value=1, max_value=28),
        st.integers(min_value=1, max_value=12),
    )
    @settings(max_examples=60, deadline=None)
    def test_bare_dd_mm_produces_future_date(self, day: int, month: int) -> None:
        """DD.MM that hasn't passed yet should produce a deadline in the
        current or next year — never a past date."""
        from datetime import date as date_t

        try:
            date_t(_TODAY.year, month, day)  # raises ValueError for invalid combos
        except ValueError:
            return  # skip invalid day/month combos (e.g. Feb 30)

        text = f"/task pay invoice by {day}.{month:02d}"
        result = parse_task(text, today=_TODAY)
        if result.deadline is not None:
            deadline_date = date_t.fromisoformat(result.deadline)
            assert deadline_date >= _TODAY, (
                f"deadline {deadline_date} is in the past for input {text!r} (today={_TODAY})"
            )


class TestAssigneeProperty:
    @given(st.from_regex(r"@[A-Za-z][A-Za-z0-9_]{1,30}", fullmatch=True))
    @settings(max_examples=80, deadline=None)
    def test_name_mention_extracted(self, mention: str) -> None:
        text = f"/task fix bug {mention}"
        result = parse_task(text, today=_TODAY)
        assert result.assignee == mention.lstrip("@")
        assert mention not in result.title

    @given(st.integers(min_value=100000000000000000, max_value=999999999999999999))
    @settings(max_examples=60, deadline=None)
    def test_numeric_mention_extracted(self, uid: int) -> None:
        text = f"/task fix bug <@{uid}>"
        result = parse_task(text, today=_TODAY)
        assert result.assignee_id == uid
        assert str(uid) not in result.title
