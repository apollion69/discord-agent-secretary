"""Shared pytest fixtures.

Anchors test time at 2026-04-21 (matches expected values:
"in 3 days" -> 2026-04-24, "за 2 дня" -> 2026-04-23). Also makes `src/`
importable so `from discord_agent_secretary.config import ...` works without
needing editable install.
"""
from __future__ import annotations

import datetime as _datetime
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Make `src/` importable without `pip install -e .`
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FROZEN_TODAY = _datetime.date(2026, 4, 21)


@pytest.fixture
def clean_settings(monkeypatch):
    """Wipe env vars that Settings reads + clear get_settings() cache."""
    for var in [
        "BACKEND",
        "DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "DISCORD_WATCH_CHANNELS",
        "MULTICA_WORKSPACE_ID", "MULTICA_DEFAULT_ASSIGNEE", "MULTICA_CLI_PATH",
        "MULTICA_CLI_TIMEOUT", "MULTICA_CLI_OUTPUT_BYTE_LIMIT",
        "BACKEND_CIRCUIT_FAILURE_THRESHOLD", "BACKEND_CIRCUIT_RESET_TIMEOUT",
        "HEALTHCHECK_PORT",
        "GITHUB_TOKEN", "GITHUB_REPO",
        "LINEAR_API_KEY", "LINEAR_TEAM_ID",
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY",
        "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
        "LOG_LEVEL", "LOG_FORMAT", "TZ",
    ]:
        monkeypatch.delenv(var, raising=False)

    from discord_agent_secretary.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def frozen_today(monkeypatch):
    """Freeze date.today() -> 2026-04-21 without requiring freezegun."""

    class _FrozenDate(_datetime.date):
        @classmethod
        def today(cls):
            return FROZEN_TODAY

    monkeypatch.setattr(
        "discord_agent_secretary.parsers.date", _FrozenDate, raising=False
    )
    return FROZEN_TODAY


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run for backend CLI calls. Success by default."""
    with patch("subprocess.run") as m:
        m.return_value = MagicMock(
            returncode=0,
            stdout=b'{"id":"VEN-42","status":"todo"}',
            stderr=b"",
        )
        yield m


@pytest.fixture
def mock_subprocess_async():
    """Mock asyncio.create_subprocess_exec for async backend calls."""
    with patch("asyncio.create_subprocess_exec") as m:
        process = MagicMock()
        async def _communicate():
            return (b'{"id":"VEN-42","status":"todo"}', b"")
        process.communicate = _communicate
        process.returncode = 0
        async def _create(*a, **kw):
            return process
        m.side_effect = _create
        yield m


@pytest.fixture(scope="session")
def multilingual_cases():
    """Load RU/EN parser golden cases from YAML."""
    path = _ROOT / "tests" / "fixtures" / "multilingual_cases.yaml"
    if not path.exists():
        return []
    with path.open() as f:
        data = yaml.safe_load(f)
    return data.get("cases", [])


@pytest.fixture
def ru_en_samples():
    return {
        "task_en": "Create authentication module",
        "task_ru": "Создать модуль аутентификации",
        "priority_ru_high": "[срочно]",
        "priority_ru_low": "[обычно]",
        "deadline_iso": "2026-04-25",
        "deadline_ru_format": "25.04.2026",
        "deadline_rel_en": "in 3 days",
        "deadline_rel_ru": "за 3 дня",
    }


@pytest.fixture
def make_discord_message():
    """Factory for a minimal mock Discord message."""

    def _make(content: str, author_id: int = 123, guild_id: int = 999):
        msg = MagicMock()
        msg.content = content
        msg.author.id = author_id
        msg.author.name = "testuser"
        msg.guild.id = guild_id
        return msg

    return _make


@pytest.fixture(autouse=True)
def test_timezone():
    os.environ["TZ"] = "Europe/Moscow"
    yield
