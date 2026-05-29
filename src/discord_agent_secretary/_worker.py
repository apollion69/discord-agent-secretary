"""Shared resilience helpers for the always-on worker run-loops.

The workers previously caught every exception, logged a flat WARNING, and
retried at the same interval forever — a persistent outage looked identical to a
one-off blip. These helpers add a consecutive-failure counter that escalates the
log level past a threshold and backs off exponentially (capped).
"""
from __future__ import annotations

import logging

BACKOFF_CAP_DEFAULT = 600.0


def backoff_seconds(base: float, failures: int, cap: float = BACKOFF_CAP_DEFAULT) -> float:
    """Exponential backoff from `base`, doubling per consecutive failure, capped."""
    if failures <= 0:
        return base
    return min(base * (2 ** (failures - 1)), cap)


def log_cycle_failure(
    logger: logging.Logger,
    worker: str,
    exc: BaseException,
    failures: int,
    threshold: int,
) -> None:
    """Log a worker cycle failure; escalate WARNING→ERROR once at/over threshold."""
    level = logging.ERROR if failures >= threshold else logging.WARNING
    logger.log(
        level,
        "%s: cycle failed",
        worker,
        extra={"detail": str(exc), "consecutive_failures": failures},
    )
