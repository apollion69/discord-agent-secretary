"""Unit tests for the shared CLI helpers."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from discord_agent_secretary._cli import run_cli_json, strip_preamble, text_or_none

pytestmark = pytest.mark.unit


class TestStripPreamble:
    def test_drops_leading_non_json(self):
        assert strip_preamble("Showing 3 issues.\n[1,2]") == "[1,2]"

    def test_object_start(self):
        assert strip_preamble("note\n{\"a\":1}") == '{"a":1}'

    def test_no_json_returns_raw(self):
        assert strip_preamble("nothing here") == "nothing here"


class TestTextOrNone:
    def test_strips(self):
        assert text_or_none("  hi  ") == "hi"

    def test_empty_is_none(self):
        assert text_or_none("   ") is None

    def test_non_str_is_none(self):
        assert text_or_none(5) is None
        assert text_or_none(None) is None


class _Proc:
    def __init__(self, out: bytes, rc: int):
        self._out = out
        self.returncode = rc
        self.stdout = self
        self.stderr = _Empty()
        self._read = out

    async def read(self):
        return self._read

    async def wait(self):
        return self.returncode


class _Empty:
    async def read(self):
        return b""


class TestRunCliJson:
    async def test_parses_json(self):
        async def fake_exec(*a, **k):
            return _Proc(b'Showing 1.\n{"issues":[]}', 0)

        with patch("discord_agent_secretary._cli.asyncio.create_subprocess_exec", side_effect=fake_exec):
            data = await run_cli_json("multica", "issue", "list", cli_timeout=5)
        assert data == {"issues": []}

    async def test_empty_returns_none(self):
        async def fake_exec(*a, **k):
            return _Proc(b"", 0)

        with patch("discord_agent_secretary._cli.asyncio.create_subprocess_exec", side_effect=fake_exec):
            assert await run_cli_json("multica", "x", cli_timeout=5) is None

    async def test_nonzero_raises(self):
        async def fake_exec(*a, **k):
            return _Proc(b"", 2)

        with patch("discord_agent_secretary._cli.asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(RuntimeError):
                await run_cli_json("multica", "x", cli_timeout=5)

    async def test_timeout_kills_and_reaps(self):
        reaped = {"wait": False}

        class _Hang:
            returncode = None

            def __init__(self):
                self.stdout = self
                self.stderr = self

            async def read(self):
                await asyncio.sleep(10)
                return b""

            def kill(self):
                self.returncode = -9

            async def wait(self):
                reaped["wait"] = True
                return self.returncode

        async def fake_exec(*a, **k):
            return _Hang()

        with patch("discord_agent_secretary._cli.asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(TimeoutError):
                await run_cli_json("multica", "x", cli_timeout=0.01)
        assert reaped["wait"] is True  # process was reaped, not left a zombie
