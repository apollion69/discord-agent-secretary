"""Shared helpers for driving the `multica` CLI from the worker loops.

These were duplicated across every worker module (`_strip_preamble` ×7, `_text`
×4, and the spawn/read/parse boilerplate). Centralizing them also fixes the two
copies (mention scanner, digest) that killed the child on timeout without reaping
it, leaking a zombie process.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


def strip_preamble(raw: str) -> str:
    """Drop leading non-JSON lines such as 'Showing 3 issues.'"""
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("{", "[")):
            return "\n".join(lines[i:])
    return raw


def text_or_none(value: object) -> str | None:
    """Normalize a value to a non-empty stripped string, else None."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


async def run_cli_json(cli_path: str, *args: str, cli_timeout: float) -> Any:
    """Run the CLI and return parsed JSON stdout (or None when empty).

    Reads stdout/stderr concurrently under a hard timeout; on timeout the child
    is killed AND reaped (`await proc.wait()`) so no zombie is left behind.
    Raises RuntimeError on non-zero exit, TimeoutError on timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        cli_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(proc.stdout.read(), proc.stderr.read()),
            timeout=cli_timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode is None:
        await proc.wait()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()[:300]
        raise RuntimeError(f"multica exit {proc.returncode}: {err}")
    text = strip_preamble(stdout.decode("utf-8", "replace")).strip()
    return json.loads(text) if text else None
