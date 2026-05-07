"""Unit tests for the resilience helpers in `backends.base`.

Covers `with_retry` (idempotent-call retry with backoff) and
`CircuitBreaker` (closed/open/half-open transitions).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from discord_agent_secretary.backends import (
    BackendCallError,
    BackendTimeoutError,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    with_retry,
)

pytestmark = pytest.mark.unit


def _factory(
    side_effects: list[Callable[[], Awaitable[object]] | BaseException | object],
) -> Callable[[], Awaitable[object]]:
    """Return a coro-factory that pops one outcome from the list per call."""
    iterator = iter(side_effects)

    async def _make() -> object:
        nxt = next(iterator)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    return _make


class TestWithRetry:
    async def test_returns_first_success(self) -> None:
        result = await with_retry(_factory(["ok"]))
        assert result == "ok"

    async def test_retries_then_succeeds(self) -> None:
        result = await with_retry(
            _factory([BackendTimeoutError("first"), "ok"]),
            attempts=2,
            initial_backoff=0.0,
        )
        assert result == "ok"

    async def test_gives_up_after_attempts(self) -> None:
        with pytest.raises(BackendTimeoutError):
            await with_retry(
                _factory(
                    [BackendTimeoutError("a"), BackendTimeoutError("b")]
                ),
                attempts=2,
                initial_backoff=0.0,
            )

    async def test_does_not_retry_outside_retry_on(self) -> None:
        # BackendCallError is NOT in the default retry_on tuple.
        with pytest.raises(BackendCallError):
            await with_retry(
                _factory([BackendCallError("nope"), "ok"]),
                attempts=2,
                initial_backoff=0.0,
            )

    async def test_backoff_sequence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []

        async def _spy(d: float) -> None:
            sleeps.append(d)

        monkeypatch.setattr(asyncio, "sleep", _spy)

        with pytest.raises(BackendTimeoutError):
            await with_retry(
                _factory(
                    [
                        BackendTimeoutError(),
                        BackendTimeoutError(),
                        BackendTimeoutError(),
                    ]
                ),
                attempts=3,
                initial_backoff=0.1,
                backoff_multiplier=2.0,
                max_backoff=10.0,
            )
        # Two sleeps between three attempts: 0.1, 0.2.
        assert sleeps == [0.1, 0.2]


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        cb.before_call()  # should not raise

    def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, reset_timeout=10.0)
        cb.on_failure()
        assert cb.state == CircuitState.CLOSED
        cb.on_failure()
        assert cb.state == CircuitState.OPEN
        with pytest.raises(CircuitOpenError):
            cb.before_call()

    def test_success_resets_failure_count(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=10.0)
        cb.on_failure()
        cb.on_failure()
        cb.on_success()
        cb.on_failure()  # would trip if counter wasn't reset
        assert cb.state == CircuitState.CLOSED

    def test_half_open_after_cooldown(self) -> None:
        clock = [0.0]
        cb = CircuitBreaker(
            failure_threshold=1,
            reset_timeout=5.0,
            clock=lambda: clock[0],
        )
        cb.on_failure()
        assert cb.state == CircuitState.OPEN
        clock[0] = 5.0
        cb.before_call()  # cool-down expired -> half-open
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_failure_reopens(self) -> None:
        clock = [0.0]
        cb = CircuitBreaker(
            failure_threshold=1,
            reset_timeout=5.0,
            clock=lambda: clock[0],
        )
        cb.on_failure()
        clock[0] = 5.0
        cb.before_call()
        assert cb.state == CircuitState.HALF_OPEN
        cb.on_failure()
        assert cb.state == CircuitState.OPEN

    def test_half_open_success_closes(self) -> None:
        clock = [0.0]
        cb = CircuitBreaker(
            failure_threshold=1,
            reset_timeout=5.0,
            clock=lambda: clock[0],
        )
        cb.on_failure()
        clock[0] = 5.0
        cb.before_call()
        cb.on_success()
        assert cb.state == CircuitState.CLOSED

    def test_threshold_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            CircuitBreaker(failure_threshold=0)
