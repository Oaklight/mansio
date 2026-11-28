"""Tests for the ``mansio_wait`` MCP tool."""

from __future__ import annotations

from typing import Any

import pytest
from mansio_client.delivery import agent as delivery_agent
from mansio_client.mcp import _DISPATCH, _TOOLS, _call_tool, _tool_wait
from mansio_core.types import Message


class _Clock:
    """A deterministic replacement for :mod:`time` in ``mcp``."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _FakeClient:
    """Return scripted poll batches and record each call."""

    def __init__(self, batches: list[list[Message]]) -> None:
        self._batches = list(batches)
        self.calls: list[tuple[str, bool]] = []

    def channel_poll(self, channel: str, *, include_own: bool = False) -> list[Message]:
        self.calls.append((channel, include_own))
        return self._batches.pop(0) if self._batches else []


def _message(payload: str = "hello") -> Message:
    return Message(
        id="msg-1",
        channel="agents",
        sender="codex-mansio-2",
        msg_type="chat",
        payload=payload,
        timestamp="2026-01-01T00:00:00Z",
    )


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    replacement = _Clock()
    monkeypatch.setattr(delivery_agent, "time", replacement)
    return replacement


def test_returns_messages_as_soon_as_they_arrive(clock: _Clock) -> None:
    client = _FakeClient([[], [_message()]])

    result = _tool_wait(client, {"channel": "agents", "timeout_seconds": 5})

    assert result["timed_out"] is False
    assert [message["payload"] for message in result["messages"]] == ["hello"]
    assert clock.sleeps == [1.0]
    assert client.calls == [("agents", False), ("agents", False)]


def test_times_out_exactly_at_the_deadline(clock: _Clock) -> None:
    client = _FakeClient([])

    result = _tool_wait(client, {"channel": "agents", "timeout_seconds": 3})

    assert result == {"messages": [], "timed_out": True}
    assert clock.sleeps == [1.0, 1.0, 1.0]
    assert clock.now == 3.0
    assert len(client.calls) == 4


def test_default_timeout_is_30_seconds(clock: _Clock) -> None:
    client = _FakeClient([])

    result = _tool_wait(client, {"channel": "agents"})

    assert result["timed_out"] is True
    assert len(clock.sleeps) == 30
    assert clock.now == 30.0


def test_zero_timeout_polls_once(clock: _Clock) -> None:
    client = _FakeClient([])

    result = _tool_wait(client, {"channel": "agents", "timeout_seconds": 0})

    assert result["timed_out"] is True
    assert clock.sleeps == []
    assert len(client.calls) == 1


@pytest.mark.parametrize("timeout", [121, 1000])
def test_rejects_a_timeout_above_the_maximum(clock: _Clock, timeout: int) -> None:
    client = _FakeClient([])

    with pytest.raises(ValueError, match="at most 120"):
        _tool_wait(client, {"channel": "agents", "timeout_seconds": timeout})

    assert client.calls == []
    assert clock.sleeps == []


def test_rejects_a_negative_timeout(clock: _Clock) -> None:
    client = _FakeClient([])

    with pytest.raises(ValueError, match="non-negative"):
        _tool_wait(client, {"channel": "agents", "timeout_seconds": -1})


def test_passes_include_own(clock: _Clock) -> None:
    client = _FakeClient([[_message()]])

    _tool_wait(client, {"channel": "agents", "timeout_seconds": 5, "include_own": True})

    assert client.calls == [("agents", True)]


def test_dispatch_and_schema() -> None:
    assert _DISPATCH["mansio_wait"] is _tool_wait
    schema = next(tool for tool in _TOOLS if tool["name"] == "mansio_wait")
    properties: dict[str, Any] = schema["inputSchema"]["properties"]
    assert schema["inputSchema"]["required"] == ["channel"]
    assert properties["timeout_seconds"]["default"] == 30
    assert properties["timeout_seconds"]["maximum"] == 120
    assert properties["include_own"]["default"] is False


def test_call_tool_returns_the_wait_shape(clock: _Clock) -> None:
    client = _FakeClient([[_message("via dispatch")]])

    result = _call_tool(client, "mansio_wait", {"channel": "agents", "timeout_seconds": 5})

    assert result["timed_out"] is False
    assert result["messages"][0]["payload"] == "via dispatch"
