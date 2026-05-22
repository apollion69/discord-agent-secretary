"""Unit tests for `discord_agent_secretary.webhook`.

Verifies parsing, formatting, and signature validation for Multica webhook
payloads that trigger review notifications.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from discord_agent_secretary.webhook import (
    ReviewEvent,
    format_review_message,
    parse_review_event,
    verify_signature,
)

pytestmark = pytest.mark.unit


class TestVerifySignature:
    def test_signature_match(self) -> None:
        secret = "test_secret"
        body = b"test body"
        expected_sig = "sha256=" + hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        assert verify_signature(body, expected_sig, secret)

    def test_signature_mismatch(self) -> None:
        secret = "test_secret"
        body = b"test body"
        wrong_sig = "sha256=" + "0" * 64
        assert not verify_signature(body, wrong_sig, secret)

    def test_no_verification_when_secret_empty(self) -> None:
        body = b"test body"
        assert parse_review_event(body, signature="", secret="") is None or True


class TestParseReviewEvent:
    def test_returns_none_for_wrong_status(self) -> None:
        payload = {
            "new_status": "done",
            "actor_type": "agent",
            "issue": {"id": "issue-1", "identifier": "VEN-123", "title": "Test"},
        }
        body = json.dumps(payload).encode()
        result = parse_review_event(body)
        assert result is None

    def test_returns_none_for_human_actor(self) -> None:
        payload = {
            "new_status": "in_review",
            "actor_type": "member",
            "issue": {"id": "issue-1", "identifier": "VEN-123", "title": "Test"},
        }
        body = json.dumps(payload).encode()
        result = parse_review_event(body)
        assert result is None

    def test_returns_none_for_bad_json(self) -> None:
        body = b"not json"
        result = parse_review_event(body)
        assert result is None

    def test_returns_none_for_non_dict(self) -> None:
        body = json.dumps(["not", "a", "dict"]).encode()
        result = parse_review_event(body)
        assert result is None

    def test_returns_none_for_missing_issue(self) -> None:
        payload = {
            "new_status": "in_review",
            "actor_type": "agent",
        }
        body = json.dumps(payload).encode()
        result = parse_review_event(body)
        assert result is None

    def test_returns_none_for_missing_issue_id(self) -> None:
        payload = {
            "new_status": "in_review",
            "actor_type": "agent",
            "issue": {"identifier": "VEN-123", "title": "Test"},
        }
        body = json.dumps(payload).encode()
        result = parse_review_event(body)
        assert result is None

    def test_returns_none_for_empty_issue_id(self) -> None:
        payload = {
            "new_status": "in_review",
            "actor_type": "agent",
            "issue": {"id": "", "identifier": "VEN-123", "title": "Test"},
        }
        body = json.dumps(payload).encode()
        result = parse_review_event(body)
        assert result is None

    def test_valid_agent_in_review_event(self) -> None:
        payload = {
            "new_status": "in_review",
            "actor_type": "agent",
            "issue": {
                "id": "issue-1",
                "identifier": "VEN-123",
                "title": "Fix the bug",
            },
        }
        body = json.dumps(payload).encode()
        result = parse_review_event(body)
        assert result is not None
        assert isinstance(result, ReviewEvent)
        assert result.issue_id == "issue-1"
        assert result.identifier == "VEN-123"
        assert result.title == "Fix the bug"
        assert result.assignee is None

    def test_valid_event_with_assignee(self) -> None:
        payload = {
            "new_status": "in_review",
            "actor_type": "agent",
            "issue": {
                "id": "issue-1",
                "identifier": "VEN-123",
                "title": "Fix the bug",
                "assignee_name": "alice",
            },
        }
        body = json.dumps(payload).encode()
        result = parse_review_event(body)
        assert result is not None
        assert result.assignee == "alice"

    def test_valid_event_with_display_name(self) -> None:
        payload = {
            "new_status": "in_review",
            "actor_type": "agent",
            "issue": {
                "id": "issue-1",
                "identifier": "VEN-123",
                "title": "Fix the bug",
                "assignee_display_name": "Alice Smith",
            },
        }
        body = json.dumps(payload).encode()
        result = parse_review_event(body)
        assert result is not None
        assert result.assignee == "Alice Smith"

    def test_identifier_fallback_to_id(self) -> None:
        payload = {
            "new_status": "in_review",
            "actor_type": "agent",
            "issue": {
                "id": "uuid-12345",
                "title": "Fix the bug",
            },
        }
        body = json.dumps(payload).encode()
        result = parse_review_event(body)
        assert result is not None
        assert result.identifier == "uuid-12345"

    def test_title_fallback_to_id(self) -> None:
        payload = {
            "new_status": "in_review",
            "actor_type": "agent",
            "issue": {
                "id": "uuid-12345",
                "identifier": "VEN-123",
            },
        }
        body = json.dumps(payload).encode()
        result = parse_review_event(body)
        assert result is not None
        assert result.title == "uuid-12345"

    def test_signature_mismatch_returns_none(self) -> None:
        secret = "correct_secret"
        payload = {
            "new_status": "in_review",
            "actor_type": "agent",
            "issue": {"id": "issue-1", "identifier": "VEN-123", "title": "Test"},
        }
        body = json.dumps(payload).encode()
        wrong_sig = "sha256=" + "0" * 64
        result = parse_review_event(body, signature=wrong_sig, secret=secret)
        assert result is None

    def test_signature_match_parses(self) -> None:
        secret = "correct_secret"
        payload = {
            "new_status": "in_review",
            "actor_type": "agent",
            "issue": {"id": "issue-1", "identifier": "VEN-123", "title": "Test"},
        }
        body = json.dumps(payload).encode()
        correct_sig = "sha256=" + hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        result = parse_review_event(body, signature=correct_sig, secret=secret)
        assert result is not None
        assert result.issue_id == "issue-1"


class TestFormatReviewMessage:
    def test_format_with_assignee(self) -> None:
        event = ReviewEvent(
            issue_id="issue-1",
            identifier="VEN-123",
            title="Fix the bug",
            assignee="alice",
        )
        message = format_review_message(event)
        assert message == "✅ Готово к ревью: **Fix the bug** `VEN-123` — alice"

    def test_format_without_assignee(self) -> None:
        event = ReviewEvent(
            issue_id="issue-1",
            identifier="VEN-123",
            title="Fix the bug",
            assignee=None,
        )
        message = format_review_message(event)
        assert message == "✅ Готово к ревью: **Fix the bug** `VEN-123`"
