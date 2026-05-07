"""Structured logging setup.

Uses structlog when available (preferred), falls back to stdlib `logging` with
a small JSON formatter when it isn't. P1 keeps the fallback path alive so the
package installs and tests run on a stock Python without extra deps.

Also exposes `SecretRedactingFilter`: a defense-in-depth filter that scrubs
known secret values from rendered log messages and string-typed extras. Wired
in via `configure_logging(secrets=[...])` from `main.py`, so an accidental
`logger.info("token=%s", settings.discord_bot_token)` cannot leak.
"""
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
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


class SecretRedactingFilter(logging.Filter):
    """Replace known secret values with a fixed mask in log records.

    Inputs are scrubbed in two places: the rendered message (after `args`
    interpolation) and every string-typed entry in `record.__dict__` (which
    is where `extra={"foo": ...}` lands). Secrets shorter than 8 chars are
    skipped to avoid pathological matches on common strings.
    """

    _MASK = "***REDACTED***"
    _MIN_LEN = 8

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # De-duplicate, drop empties/short values, and sort longest-first so
        # nested matches don't leave a partial leak when one secret is a
        # prefix of another.
        self._secrets: list[str] = sorted(
            {s for s in secrets if s and len(s) >= self._MIN_LEN},
            key=len,
            reverse=True,
        )

    def _scrub(self, value: str) -> str:
        for s in self._secrets:
            if s in value:
                value = value.replace(s, self._MASK)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError):
            rendered = str(record.msg)
        scrubbed = self._scrub(rendered)
        if scrubbed != rendered:
            record.msg = scrubbed
            record.args = ()
        for key, val in list(record.__dict__.items()):
            if isinstance(val, str):
                record.__dict__[key] = self._scrub(val)
        return True


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    *,
    force: bool = False,
    secrets: Iterable[str] | None = None,
) -> None:
    """Install handlers on the root logger.

    Idempotent by default — call again with `force=True` to re-configure
    (tests do this between cases). When `secrets` is non-empty a
    `SecretRedactingFilter` is attached to the handler so any accidental
    inclusion of those values in log records is masked.
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

    if secrets:
        handler.addFilter(SecretRedactingFilter(secrets))

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
