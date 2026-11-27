"""Typed SSE warnings: transport routing, history, and SDK exposure."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import pytest
from mansio_client._vendor.sse import EventSource
from mansio_client.client import MansioClient
from mansio_client.transport.http import WARNING_HISTORY, HttpTransport
from mansio_core.contracts import WarningEvent, warning_event_to_json
from mansio_core.transport import WarningSource

_MESSAGE_DATA = json.dumps(
    {
        "channel": "general",
        "message": {
            "id": "msg-1",
            "channel": "general",
            "sender": "agent-b",
            "msg_type": "chat",
            "payload": "hi",
            "timestamp": "2026-01-01T00:00:00Z",
        },
    }
)


def _transport() -> HttpTransport:
    return HttpTransport("http://localhost:8742", user_id="agent-a")


class TestMixedStream:
    def test_comment_warning_and_message_records(self) -> None:
        lines = [
            ": connected",
            "",
            "event: warning",
            'data: {"kind": "dropped", "count": 2, "channel": null}',
            "",
            ": keepalive",
            "",
            "id: msg-1",
            f"data: {_MESSAGE_DATA}",
            "",
        ]
        events = list(EventSource(lines))
        assert [(event.event, event.id) for event in events] == [
            ("warning", ""),
            ("message", "msg-1"),
        ]

        transport = _transport()
        seen: list[WarningEvent] = []
        transport.warning_callback = seen.append
        for event in events:
            transport._handle_sse_event(event)

        expected = WarningEvent(kind="dropped", count=2)
        assert seen == [expected]
        assert transport.recent_warnings == (expected,)
        assert transport._last_event_id == "msg-1"


class TestWarningHistory:
    def test_history_is_bounded(self) -> None:
        transport = _transport()
        transport.warning_callback = lambda _warning: None
        for count in range(1, WARNING_HISTORY + 6):
            transport._handle_warning_record(
                warning_event_to_json(WarningEvent(kind="dropped", count=count))
            )
        recent = transport.recent_warnings
        assert len(recent) == WARNING_HISTORY
        assert recent[0].count == 6
        assert recent[-1].count == WARNING_HISTORY + 5

    def test_malformed_warning_is_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        transport = _transport()
        with caplog.at_level(logging.WARNING, logger="mansio_client.transport.http"):
            transport._handle_warning_record("{not json")
        assert transport.recent_warnings == ()
        assert "invalid SSE warning event" in caplog.text

        transport._handle_sse_event(
            next(iter(EventSource(["id: msg-9", f"data: {_MESSAGE_DATA}", ""])))
        )
        assert transport._last_event_id == "msg-9"

    def test_callback_exception_does_not_escape(self) -> None:
        transport = _transport()

        def boom(_warning: WarningEvent) -> None:
            raise RuntimeError("callback failed")

        transport.warning_callback = boom
        transport._handle_warning_record(
            warning_event_to_json(WarningEvent(kind="truncated", count=500, channel="general"))
        )
        assert transport.recent_warnings == (
            WarningEvent(kind="truncated", count=500, channel="general"),
        )


class _StubTransport:
    warning_callback: Callable[[WarningEvent], None] | None = None


class _PlainTransport:
    """A transport with no warning support at all."""


def _client_with(monkeypatch: pytest.MonkeyPatch, transport: object) -> MansioClient:
    monkeypatch.setattr("mansio_client.client.create_transport", lambda *args, **kwargs: transport)
    monkeypatch.setattr(MansioClient, "_announce", lambda self: None)
    monkeypatch.setattr(MansioClient, "_restore_cursors", lambda self: None)
    return MansioClient("http://localhost:8742", "agent-a")


def test_client_owns_warning_history(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    stub = _StubTransport()
    client = _client_with(monkeypatch, stub)

    assert isinstance(stub, WarningSource)
    callback = stub.warning_callback
    assert callback == client._on_transport_warning
    assert callback is not None
    assert client.recent_warnings == ()

    warning = WarningEvent(kind="truncated", count=500, channel="general")
    with caplog.at_level(logging.WARNING, logger="mansio_client.client"):
        callback(warning)

    assert client.recent_warnings == (warning,)
    assert "SSE warning: truncated" in caplog.text


def test_client_without_warning_source_still_constructs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _PlainTransport()
    client = _client_with(monkeypatch, stub)

    assert not isinstance(stub, WarningSource)
    assert not hasattr(stub, "warning_callback")
    assert client.recent_warnings == ()
