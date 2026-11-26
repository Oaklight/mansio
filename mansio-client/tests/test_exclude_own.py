"""Tests for excluding a client's own messages (issue #265).

``channel_poll()`` and ``subscribe()`` omit messages the client sent by
default; ``channel_read()`` keeps them unless asked not to, because the
note/thought/memory helpers read the agent's own notebook through it.
"""

from __future__ import annotations

import json

from mansio_client import MansioClient
from mansio_client.transport import HttpTransport, _Subscription
from mansio_client.types import Message


def _msg(message_id: str, sender: str, payload: str) -> Message:
    return Message(
        id=message_id,
        channel="ch",
        sender=sender,
        msg_type="chat",
        payload=payload,
        timestamp="2026-01-01T00:00:00Z",
    )


class _FakeTransport:
    """Minimal transport returning a fixed message list."""

    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages
        self.published: list[tuple] = []

    def query(self, channel: str, **kwargs):
        if channel.startswith("_system:cursors:"):
            return []
        return list(self.messages)

    def channels(self, *, detail: bool = False):
        return ["ch"]

    def publish(self, *args, **kwargs):
        self.published.append((args, kwargs))
        return "cursor-id"


def _client(messages: list[Message]) -> MansioClient:
    client = MansioClient.__new__(MansioClient)
    client._user_id = "me"
    client._display_name = "me"
    client._cursors = {}
    client._persisted = {}
    client._transport = _FakeTransport(messages)
    return client


class TestClientFilters:
    def test_poll_excludes_own_by_default(self) -> None:
        client = _client([_msg("1", "me", "mine"), _msg("2", "other", "theirs")])
        assert [m.payload for m in client.channel_poll("ch")] == ["theirs"]

    def test_poll_include_own(self) -> None:
        client = _client([_msg("1", "me", "mine"), _msg("2", "other", "theirs")])
        assert [m.payload for m in client.channel_poll("ch", include_own=True)] == [
            "mine",
            "theirs",
        ]

    def test_read_defaults_to_include_own(self) -> None:
        client = _client([_msg("1", "me", "mine"), _msg("2", "other", "theirs")])
        assert {m.payload for m in client.channel_read("ch")} == {"mine", "theirs"}
        assert [m.payload for m in client.channel_read("ch", include_own=False)] == [
            "theirs"
        ]

    def test_check_unread_excludes_own_by_default(self) -> None:
        client = _client([_msg("1", "me", "mine"), _msg("2", "other", "theirs")])
        assert client.check_unread() == {"ch": 1}
        assert client.check_unread(include_own=True) == {"ch": 2}


class _RecordingTransport:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def subscribe(self, channel, callback, on_error=None, **kwargs):
        self.kwargs = kwargs
        return "sid"

    def unsubscribe(self, subscription_id: str) -> None:
        pass


def test_subscribe_forwards_include_own() -> None:
    client = MansioClient.__new__(MansioClient)
    client._user_id = "me"
    client._transport = _RecordingTransport()

    assert client.subscribe("ch", lambda m: None) == "sid"
    assert client._transport.kwargs == {"include_own": False}

    client.subscribe("ch", lambda m: None, include_own=True)
    assert client._transport.kwargs == {"include_own": True}


class TestTransportDispatchFilter:
    def _event(self, sender: str) -> str:
        return json.dumps(
            {
                "channel": "ch",
                "message": {
                    "id": "m1",
                    "channel": "ch",
                    "sender": sender,
                    "msg_type": "chat",
                    "payload": "p",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
            }
        )

    def test_excludes_own_when_include_own_false(self) -> None:
        transport = HttpTransport("http://example.invalid", user_id="me")
        received: list[Message] = []
        transport._sse_callbacks = {
            "ch": {"s1": _Subscription(received.append, None, False)}
        }
        try:
            transport._dispatch_sse_event(self._event("me"))
            assert received == []
        finally:
            transport.close()

    def test_delivers_own_when_include_own_true(self) -> None:
        transport = HttpTransport("http://example.invalid", user_id="me")
        received: list[Message] = []
        transport._sse_callbacks = {
            "ch": {"s1": _Subscription(received.append, None, True)}
        }
        try:
            transport._dispatch_sse_event(self._event("me"))
            assert [m.payload for m in received] == ["p"]
        finally:
            transport.close()
