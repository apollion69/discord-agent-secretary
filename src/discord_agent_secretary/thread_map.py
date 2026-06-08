"""Persistent bidirectional issue↔thread map.

Bidirectional sync needs to know, for a tracker comment, which Discord thread to
post it in — and, for a thread reply, which issue to comment on. This stores
that mapping in an atomically-written JSON file (same pattern as the poller's
`seen.json`). Insertion-ordered with a cap so it can't grow without bound on a
long-running bot.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_MAX = 5000


class ThreadMap:
    """In-memory bidirectional map, persisted to JSON on every mutation."""

    def __init__(self, path: Path, *, max_entries: int = _DEFAULT_MAX) -> None:
        self._path = path
        self._max = max_entries
        self._issue_to_thread: dict[str, int] = {}
        self._thread_to_issue: dict[int, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        raw = data.get("issue_to_thread", {}) if isinstance(data, dict) else {}
        for issue_id, thread_id in raw.items():
            if isinstance(issue_id, str) and isinstance(thread_id, int):
                self._issue_to_thread[issue_id] = thread_id
                self._thread_to_issue[thread_id] = issue_id

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"issue_to_thread": self._issue_to_thread}), encoding="utf-8"
        )
        tmp.replace(self._path)

    def set(self, issue_id: str, thread_id: int) -> None:
        """Record (issue ↔ thread); evict the oldest pair past the cap."""
        # Drop any stale reverse entry for an issue that moves threads.
        old_thread = self._issue_to_thread.get(issue_id)
        if old_thread is not None and old_thread != thread_id:
            self._thread_to_issue.pop(old_thread, None)
        self._issue_to_thread[issue_id] = thread_id
        self._thread_to_issue[thread_id] = issue_id
        while len(self._issue_to_thread) > self._max:
            oldest, t = next(iter(self._issue_to_thread.items()))
            self._issue_to_thread.pop(oldest, None)
            self._thread_to_issue.pop(t, None)
        self._save()

    def thread_for_issue(self, issue_id: str) -> int | None:
        return self._issue_to_thread.get(issue_id)

    def issue_for_thread(self, thread_id: int) -> str | None:
        return self._thread_to_issue.get(thread_id)

    def __len__(self) -> int:
        return len(self._issue_to_thread)
