"""Structured logging setup.

Uses structlog when available (preferred), falls back to stdlib `logging` with
a small JSON formatter when it isn't. P1 keeps the fallback path alive so the
package installs and tests run on a stock Python without extra deps.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Minimal single-line JSON formatter for stdlib logging fallback."""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    *,
    force: bool = False,
) -> None:
    """Install handlers on the root logger.

    Idempotent by default — call again with `force=True` to re-configure
    (tests do this between cases).
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s")
        )

    root.addHandler(handler)
    root.setLevel(level)

    try:
        import structlog

        structlog.configure(
            processors=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                (
                    structlog.processors.JSONRenderer(ensure_ascii=False)
                    if fmt == "json"
                    else structlog.dev.ConsoleRenderer(colors=False)
                ),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, level, logging.INFO)
            ),
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
    except ImportError:
        pass

    _CONFIGURED = True


def get_logger(name: str) -> Any:
    """Return a structured logger. Prefers structlog, else stdlib."""
    try:
        import structlog

        return structlog.get_logger(name)
    except ImportError:
        return logging.getLogger(name)
