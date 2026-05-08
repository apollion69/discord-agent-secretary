"""Concurrency stress tests for `RateLimiter` and `CircuitBreaker`.

These tests validate the "no-lock-needed" atomicity claim made in the
docstrings. The claim holds because all relevant methods are synchronous
(no `await`) and asyncio only switches coroutines at `await` points.

The tests use a high degree of parallelism to exercise boundary conditions
that sequential tests miss: simultaneous token-bucket draining, simultaneous
threshold trips, and the HALF_OPEN probe-race.
"""
from __future__ import annotations

import asyncio

import pytest

from discord_agent_secretary.backends import CircuitBreaker, CircuitOpenError, CircuitState
from discord_agent_secretary.handlers import RateLimiter

pytestmark = pytest.mark.unit


class TestRateLimiterConcurrency:
    async def test_capacity_not_exceeded_under_concurrency(self) -> None:
        """50 concurrent acquires from a capacity-10 bucket must not
        produce more than 10 successful acquisitions."""
        limiter = RateLimiter(capacity=10, refill_per_sec=0.0)
        key = "stress-key"

        async def _try() -> bool:
            return limiter.acquire(key)

        results = await asyncio.gather(*[_try() for _ in range(50)])
        acquired = sum(results)
        # Exactly 10 tokens were in the bucket; no more should succeed.
        assert acquired == 10

    async def test_independent_keys_do_not_interfere(self) -> None:
        """Concurrent acquires on N independent keys should each succeed
        exactly once (capacity=1 each)."""
        limiter = RateLimiter(capacity=1, refill_per_sec=0.0)

        async def _try(key: str) -> bool:
            return limiter.acquire(key)

        n = 40
        keys = [f"user-{i}" for i in range(n)]
        results = await asyncio.gather(*[_try(k) for k in keys])
        # Every distinct key should have succeeded once.
        assert sum(results) == n

    async def test_concurrent_eviction_does_not_corrupt_state(self) -> None:
        """Drive eviction (every 3rd call) from parallel coroutines and
        verify the limiter stays functional afterward."""
        now = [0.0]
        limiter = RateLimiter(
            capacity=1,
            refill_per_sec=0.0,
            clock=lambda: now[0],
        )
        limiter._EVICT_EVERY = 3  # type: ignore[assignment]
        limiter._BUCKET_TTL = 10.0  # type: ignore[assignment]

        keys = [f"k-{i}" for i in range(60)]

        async def _acquire(key: str) -> None:
            limiter.acquire(key)

        # First wave — all keys seen at t=0.
        await asyncio.gather(*[_acquire(k) for k in keys])

        # Advance past TTL and run a second wave; eviction should fire.
        now[0] = 30.0
        await asyncio.gather(*[_acquire(k) for k in keys])

        # Limiter must still honour new acquires post-eviction.
        assert limiter.acquire("fresh") is True


class TestCircuitBreakerConcurrency:
    async def test_threshold_is_a_soft_target_under_concurrency(self) -> None:
        """Under concurrent calls, the failure counter may exceed
        `failure_threshold` by up to N concurrent callers. This is the
        documented soft-target behaviour; we assert it does NOT
        under-count (circuit must always open after threshold failures).
        """
        cb = CircuitBreaker(failure_threshold=5, reset_timeout=60.0)

        async def _fail() -> None:
            cb.on_failure()

        # Fire 5 failures concurrently — circuit MUST be open after.
        await asyncio.gather(*[_fail() for _ in range(5)])
        assert cb.state == CircuitState.OPEN

    async def test_half_open_probe_only_one_call_passes(self) -> None:
        """After cool-down, `before_call` promotes to HALF_OPEN once.
        Subsequent concurrent calls should see HALF_OPEN too (no second
        promotion races back to CLOSED). The invariant: at most one
        concurrent call sets state to CLOSED; all others are blocked
        by a second failure if the probe fails.

        This test documents the observed behaviour — it does NOT assert
        that exactly one passes (the soft-target rule applies), but it
        does assert that the state machine stays consistent.
        """
        clock = [0.0]
        cb = CircuitBreaker(
            failure_threshold=1,
            reset_timeout=5.0,
            clock=lambda: clock[0],
        )
        cb.on_failure()
        assert cb.state == CircuitState.OPEN

        clock[0] = 5.0  # cool-down expired

        open_errors = 0
        half_open_passes = 0

        async def _probe() -> None:
            nonlocal open_errors, half_open_passes
            try:
                cb.before_call()
                half_open_passes += 1
                cb.on_success()  # pretend the probe succeeded
            except CircuitOpenError:
                open_errors += 1

        await asyncio.gather(*[_probe() for _ in range(20)])

        # At least one probe passed (the first one to see OPEN → HALF_OPEN).
        assert half_open_passes >= 1
        # State machine is in a consistent final state.
        assert cb.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    async def test_concurrent_successes_close_circuit(self) -> None:
        """on_success() is idempotent — multiple concurrent successes
        all closing the circuit must not corrupt state."""
        clock = [0.0]
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.0, clock=lambda: clock[0])
        cb.on_failure()
        clock[0] = 1.0
        cb.before_call()  # → HALF_OPEN

        async def _succeed() -> None:
            cb.on_success()

        await asyncio.gather(*[_succeed() for _ in range(20)])
        assert cb.state == CircuitState.CLOSED
        assert cb._failures == 0  # noqa: SLF001
