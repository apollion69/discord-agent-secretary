"""Unit tests for discord_agent_secretary.message_router."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from discord_agent_secretary.message_router import register_message_dispatch

pytestmark = pytest.mark.unit


def _capturing_client() -> tuple[MagicMock, dict]:
    client = MagicMock()
    captured: dict[str, object] = {}

    def fake_event(coro: object) -> object:
        captured["on_message"] = coro
        return coro

    client.event = fake_event
    return client, captured


class TestRegisterMessageDispatch:
    async def test_fans_out_to_all_handlers(self) -> None:
        client, captured = _capturing_client()
        calls: list[str] = []

        async def h1(_m: object) -> None:
            calls.append("h1")

        async def h2(_m: object) -> None:
            calls.append("h2")

        register_message_dispatch(client, [h1, h2])
        await captured["on_message"]("MSG")  # type: ignore[operator]
        assert calls == ["h1", "h2"]

    async def test_one_handler_raising_does_not_stop_others(self) -> None:
        client, captured = _capturing_client()
        calls: list[str] = []

        async def boom(_m: object) -> None:
            raise RuntimeError("handler blew up")

        async def ok(_m: object) -> None:
            calls.append("ok")

        register_message_dispatch(client, [boom, ok])
        # Must not raise; the second handler still runs.
        await captured["on_message"]("MSG")  # type: ignore[operator]
        assert calls == ["ok"]

    async def test_empty_handler_list_is_noop(self) -> None:
        client, captured = _capturing_client()
        register_message_dispatch(client, [])
        await captured["on_message"]("MSG")  # type: ignore[operator]  # no error
