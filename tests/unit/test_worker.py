"""Unit tests for the shared worker resilience helpers."""
from __future__ import annotations

import logging

import pytest

from discord_agent_secretary._worker import backoff_seconds, log_cycle_failure

pytestmark = pytest.mark.unit


class TestBackoff:
    def test_no_failures_returns_base(self):
        assert backoff_seconds(30.0, 0) == 30.0

    def test_doubles_per_failure(self):
        assert backoff_seconds(30.0, 1) == 30.0
        assert backoff_seconds(30.0, 2) == 60.0
        assert backoff_seconds(30.0, 3) == 120.0

    def test_capped(self):
        assert backoff_seconds(30.0, 20, cap=600.0) == 600.0


class TestLogCycleFailure:
    def test_warning_below_threshold(self, caplog):
        with caplog.at_level(logging.WARNING):
            log_cycle_failure(logging.getLogger("w"), "w", RuntimeError("x"), failures=1, threshold=3)
        assert caplog.records[-1].levelno == logging.WARNING

    def test_error_at_threshold(self, caplog):
        with caplog.at_level(logging.WARNING):
            log_cycle_failure(logging.getLogger("w"), "w", RuntimeError("x"), failures=3, threshold=3)
        assert caplog.records[-1].levelno == logging.ERROR
