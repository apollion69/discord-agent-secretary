"""Unit tests for discord_agent_secretary.logging_setup."""
from __future__ import annotations

import json
import logging

import pytest

from discord_agent_secretary import logging_setup
from discord_agent_secretary.logging_setup import (
    JsonFormatter,
    SecretRedactingFilter,
    configure_logging,
    get_logger,
)


@pytest.fixture(autouse=True)
def reset_logging_module():
    """Force-reset the module between tests so configure_logging() always runs."""
    logging_setup._CONFIGURED = False
    yield
    logging_setup._CONFIGURED = False
    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)


class TestJsonFormatter:
    def test_basic_message_fields(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="x.py", lineno=1, msg="hello", args=None, exc_info=None,
        )
        payload = json.loads(fmt.format(record))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "test.logger"
        assert payload["msg"] == "hello"
        assert "ts" in payload

    def test_extra_fields_included(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="t", level=logging.WARNING, pathname="x.py", lineno=1,
            msg="boom", args=None, exc_info=None,
        )
        record.issue_id = "VEN-42"
        record.user = "alice"
        payload = json.loads(fmt.format(record))
        assert payload["issue_id"] == "VEN-42"
        assert payload["user"] == "alice"

    def test_unicode_preserved(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname="x.py", lineno=1,
            msg="Привет мир", args=None, exc_info=None,
        )
        payload = json.loads(fmt.format(record))
        assert payload["msg"] == "Привет мир"


class TestConfigureLogging:
    def test_applies_level(self):
        configure_logging(level="DEBUG", fmt="json")
        assert logging.getLogger().level == logging.DEBUG

    def test_idempotent_by_default(self):
        configure_logging(level="INFO", fmt="json")
        handler_count_1 = len(logging.getLogger().handlers)
        configure_logging(level="DEBUG", fmt="json")
        handler_count_2 = len(logging.getLogger().handlers)
        assert handler_count_1 == handler_count_2

    def test_force_reconfigures(self):
        configure_logging(level="INFO", fmt="json")
        configure_logging(level="DEBUG", fmt="json", force=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_json_format_produces_valid_json(self, capsys):
        configure_logging(level="INFO", fmt="json", force=True)
        logging.getLogger("t").info("pinged", extra={"trace_id": "abc123"})
        captured = capsys.readouterr().out.strip()
        payload = json.loads(captured)
        assert payload["msg"] == "pinged"
        assert payload["trace_id"] == "abc123"

    def test_console_format_not_json(self, capsys):
        configure_logging(level="INFO", fmt="console", force=True)
        logging.getLogger("t").info("pinged")
        captured = capsys.readouterr().out.strip()
        assert "pinged" in captured
        with pytest.raises(json.JSONDecodeError):
            json.loads(captured)


class TestGetLogger:
    def test_returns_a_logger(self):
        logger = get_logger("discord_agent_secretary.test")
        assert logger is not None
        assert hasattr(logger, "info")


class TestSecretRedactingFilter:
    def test_redacts_message_arg(self, capsys):
        secret = "tok_supersecret_value_123"
        configure_logging(level="INFO", fmt="json", force=True, secrets=[secret])
        logging.getLogger("t").info("sending request with token=%s", secret)
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert secret not in payload["msg"]
        assert "***REDACTED***" in payload["msg"]

    def test_redacts_extra_field(self, capsys):
        secret = "ghp_redactable_pat_token_abc"
        configure_logging(level="INFO", fmt="json", force=True, secrets=[secret])
        logging.getLogger("t").error("api call failed", extra={"detail": secret})
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert secret not in out
        assert payload["detail"] == "***REDACTED***"

    def test_short_secrets_skipped(self):
        # Short values are unsafe to scrub: "x" or "abc" would mask half the
        # English language. The filter ignores anything below MIN_LEN.
        f = SecretRedactingFilter(["abc"])
        record = logging.LogRecord(
            "t", logging.INFO, "x.py", 1, "abc DEF", None, None
        )
        f.filter(record)
        assert "abc" in record.getMessage()

    def test_empty_secrets_is_passthrough(self, capsys):
        configure_logging(level="INFO", fmt="json", force=True, secrets=[])
        logging.getLogger("t").info("just a message")
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload["msg"] == "just a message"

    def test_secret_embedded_in_longer_string_still_redacted(self):
        # Substring mutation guard: a future change from `if s in value` to
        # `if s == value` would let secrets leak inside longer log lines.
        # This test pins the substring-match contract.
        secret = "abcdefgh-supersecret-xyz12"
        f = SecretRedactingFilter([secret])
        embedded = f"prefix_{secret}_suffix"
        record = logging.LogRecord(
            "t", logging.INFO, "x.py", 1, embedded, None, None
        )
        f.filter(record)
        rendered = record.getMessage()
        assert secret not in rendered
        assert "***REDACTED***" in rendered

    def test_longest_secret_redacted_first(self):
        # When two secrets overlap (one is a prefix of the other), the
        # longer one must be processed first to avoid leaving a partial
        # leak. This pins the longest-first sort contract.
        short = "abcdefgh"
        long = "abcdefgh_PRIVATE_TAIL_XYZ"
        f = SecretRedactingFilter([short, long])
        record = logging.LogRecord(
            "t", logging.INFO, "x.py", 1, f"see {long} here", None, None
        )
        f.filter(record)
        rendered = record.getMessage()
        # Short token must not appear bare — would indicate the short-first
        # order chopped the long token in two.
        assert short not in rendered
        assert long not in rendered
        assert rendered.count("***REDACTED***") == 1
