"""Tests for NATSBackend's bounded message cache and stream fallback.

Covers issue #294: the in-process message index grew without bound on a
long-lived server, and ``get_message()`` could not find messages stored
by a sibling server or before a restart. The cache is now LRU-bounded and
a miss falls back to scanning the JetStream stream.

No NATS server is required: the JetStream context is stubbed, in the same
style as test_nats_backend_info_guard.py.
"""

from __future__ import annotations

import asyncio

import pytest

nats_py = pytest.importorskip("nats")  # noqa: E402

import nats.errors  # noqa: E402
from mansio_core.backends.nats import NATSBackend  # noqa: E402
from mansio_core.types import Message  # noqa: E402


def _message(message_id: str, channel: str = "general", sender: str = "agent-a") -> Message:
    return Message(
        id=message_id,
        channel=channel,
        sender=sender,
        msg_type="text",
        payload=f"payload-{message_id}",
        timestamp="2026-01-01T00:00:00+00:00",
    )


class _RawMessage:
    """Minimal stand-in for nats.aio.msg.Msg: only ``.data`` is read."""

    def __init__(self, data: bytes) -> None:
        self.data = data


class _FakePullSub:
    """Pull subscription that replays prepared batches, then times out."""

    def __init__(self, batches: list[list[bytes]]) -> None:
        self._batches = list(batches)
        self.unsubscribed = False

    async def fetch(self, batch: int, timeout: float) -> list[_RawMessage]:
        if not self._batches:
            raise nats.errors.TimeoutError()
        return [_RawMessage(data) for data in self._batches.pop(0)]

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _FakeJetStream:
    """JetStreamContext stand-in covering only pull_subscribe."""

    def __init__(self, batches: list[list[bytes]]) -> None:
        self.sub = _FakePullSub(batches)
        self.subjects: list[str] = []

    async def pull_subscribe(self, subject: str, stream: str | None = None) -> _FakePullSub:
        self.subjects.append(subject)
        return self.sub


class _ExplodingJetStream:
    """Fails loudly if the cache path touches NATS at all."""

    async def pull_subscribe(self, subject: str, stream: str | None = None) -> None:
        raise AssertionError("stream scan must not run on a cache hit")


def _wire(backend: NATSBackend, js: object) -> None:
    """Mark *backend* connected with a fake JetStream and a local runner."""
    backend._connected = True
    backend._js = js
    backend._run_async = lambda coro, timeout=None: asyncio.run(coro)  # type: ignore[method-assign]


def _payloads(*messages: Message) -> list[bytes]:
    return [NATSBackend._msg_to_nats_payload(m) for m in messages]


class TestBoundedCache:
    def test_oldest_entry_is_evicted_at_the_cap(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_INDEX", message_index_size=2)

        backend._remember(_message("m1"))
        backend._remember(_message("m2"))
        backend._remember(_message("m3"))

        assert list(backend._messages_by_id) == ["m2", "m3"]

    def test_zero_means_unlimited(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_INDEX", message_index_size=0)

        for i in range(50):
            backend._remember(_message(f"m{i}"))

        assert len(backend._messages_by_id) == 50

    def test_negative_size_is_rejected(self):
        with pytest.raises(ValueError, match="message_index_size"):
            NATSBackend(stream_name="MANSIO_TEST_INDEX", message_index_size=-1)

    def test_cache_hit_refreshes_lru_position(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_INDEX", message_index_size=2)
        backend._remember(_message("m1"))
        backend._remember(_message("m2"))

        # Touching m1 makes m2 the least recently used entry.
        backend._remember(_message("m1"))
        backend._remember(_message("m3"))

        assert list(backend._messages_by_id) == ["m1", "m3"]


class TestGetMessageFallback:
    def test_cache_hit_never_touches_nats(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_GET")
        message = _message("m1")
        backend._remember(message)
        _wire(backend, _ExplodingJetStream())

        assert backend.get_message("m1") is message

    def test_miss_is_answered_from_the_stream(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_GET")
        target = _message("target")
        js = _FakeJetStream([_payloads(_message("other"), target)])
        _wire(backend, js)

        found = backend.get_message("target")

        assert found is not None
        assert found.id == "target"
        assert list(backend._messages_by_id) == ["target"]
        assert js.subjects == ["mansio.>"]

    def test_evicted_message_is_still_found(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_GET", message_index_size=1)
        first = _message("m1")
        backend._remember(first)
        backend._remember(_message("m2"))
        assert "m1" not in backend._messages_by_id

        js = _FakeJetStream([_payloads(first)])
        _wire(backend, js)

        found = backend.get_message("m1")

        assert found is not None
        assert found.id == "m1"

    def test_scan_continues_across_batches(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_GET")
        target = _message("target")
        js = _FakeJetStream(
            [
                _payloads(_message("a"), _message("b")),
                _payloads(target),
            ]
        )
        _wire(backend, js)

        assert backend.get_message("target") is not None

    def test_undecodable_payloads_are_skipped(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_GET")
        target = _message("target")
        js = _FakeJetStream([[b"not json at all", *_payloads(target)]])
        _wire(backend, js)

        assert backend.get_message("target") is not None

    def test_unknown_message_returns_none(self):
        backend = NATSBackend(stream_name="MANSIO_TEST_GET")
        js = _FakeJetStream([_payloads(_message("present"))])
        _wire(backend, js)

        assert backend.get_message("missing") is None
