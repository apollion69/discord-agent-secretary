"""Regex-first parser for `/task`-style natural-language commands (RU + EN).

Follows the pipeline rule in `~/.claude/rules/common/llm-text-processing.md`:
regex pre-extracts structured fields (priority, deadline, assignee) and the
remaining text is the title. The LLM never touches high-confidence cases.

Frozen dataclass output — immutable so callers can freely pass it around.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final


@dataclass(frozen=True)
class ParsedTask:
    """Structured result of parsing a `/task` command.

    `assignee` is a Discord-visible handle (letters/digits/underscore).
    `assignee_id` is a numeric ID extracted from `<@123456789>` mentions.
    At most one of the two is set in practice (mention formats are exclusive).
    """

    title: str
    priority: str | None = None
    deadline: str | None = None
    assignee: str | None = None
    assignee_id: int | None = None


_CMD_PREFIX: Final = re.compile(r"^\s*/task\s+", re.IGNORECASE)

_PRIORITY_EN: Final = re.compile(r"\[\s*P([123])\s*\]", re.IGNORECASE)
_PRIORITY_RU: Final = re.compile(r"\[\s*(срочно|обычно)\s*\]", re.IGNORECASE)

_DATE_ISO: Final = re.compile(r"(?:\bby\s+)?(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
_DATE_RU_FULL: Final = re.compile(r"(?:\bby\s+)?(\d{1,2})\.(\d{1,2})\.(\d{4})")
_DATE_RU_SHORT: Final = re.compile(r"(?:\bby\s+)?\b(\d{1,2})\.(\d{1,2})(?![.\d])")
_REL_EN: Final = re.compile(r"\bin\s+(\d+)\s+days?\b", re.IGNORECASE)
_REL_RU: Final = re.compile(r"\bза\s+(\d+)\s+(?:дня|дней|день)\b", re.IGNORECASE)

_ASSIGNEE_NUMERIC: Final = re.compile(r"<@!?(\d+)>")
_ASSIGNEE_NAME: Final = re.compile(r"(?<![\w@])@([A-Za-z0-9_]+)")

_PRIORITY_EN_MAP: Final = {"1": "high", "2": "medium", "3": "low"}
_PRIORITY_RU_MAP: Final = {"срочно": "high", "обычно": "medium"}

_WS: Final = re.compile(r"\s+")


def _cut(body: str, match: re.Match[str]) -> str:
    """Remove a matched span and return the new body."""
    return body[: match.start()] + body[match.end() :]


def parse_task(text: str, *, today: date | None = None) -> ParsedTask:
    """Parse a `/task`-style command into structured fields.

    Unmatched text becomes the title (whitespace-collapsed). `today` is
    overridable for deterministic relative-date tests (see conftest
    `frozen_today` fixture).
    """
    if today is None:
        today = date.today()

    body = _CMD_PREFIX.sub("", text, count=1).strip()

    priority: str | None = None
    deadline: str | None = None
    assignee: str | None = None
    assignee_id: int | None = None

    if m := _PRIORITY_EN.search(body):
        priority = _PRIORITY_EN_MAP[m.group(1)]
        body = _cut(body, m)
    elif m := _PRIORITY_RU.search(body):
        priority = _PRIORITY_RU_MAP[m.group(1).lower()]
        body = _cut(body, m)

    if m := _DATE_ISO.search(body):
        deadline = m.group(1)
        body = _cut(body, m)
    elif m := _DATE_RU_FULL.search(body):
        deadline = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        body = _cut(body, m)
    elif m := _DATE_RU_SHORT.search(body):
        deadline = f"{today.year}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        body = _cut(body, m)
    elif m := _REL_EN.search(body):
        deadline = (today + timedelta(days=int(m.group(1)))).isoformat()
        body = _cut(body, m)
    elif m := _REL_RU.search(body):
        deadline = (today + timedelta(days=int(m.group(1)))).isoformat()
        body = _cut(body, m)

    if m := _ASSIGNEE_NUMERIC.search(body):
        assignee_id = int(m.group(1))
        body = _cut(body, m)
    elif m := _ASSIGNEE_NAME.search(body):
        assignee = m.group(1)
        body = _cut(body, m)

    title = _WS.sub(" ", body).strip()

    return ParsedTask(
        title=title,
        priority=priority,
        deadline=deadline,
        assignee=assignee,
        assignee_id=assignee_id,
    )
