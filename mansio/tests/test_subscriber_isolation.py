"""A throwing subscriber must not take down publishing or watch delivery.

Before the fix, a callback that raised propagated out of ``Bus.publish()``
after the message had already been stored, so the publish call failed; the
same exception could also escape a backend watch thread, which has no handler
and therefore dies.  Both delivery paths must isolate each callback.
"""

from __future__ import annotations

from collections.abc import Callable

from mansio import Bus, MemoryBackend
from mansio.types import Message


class _WatchingMemoryBackend(MemoryBackend):
    """MemoryBackend that also satisfies the ``Watchable`` protocol."""

    def __init__(self) -> None:
        super().__init__()
        self.watches: dict[str, tuple[str, Callable[[Message], None]]] = {}
        self._next_watch = 0

    def watch(self, channel: str, callback: Callable[[Message], None]) -> str:
        self._next_watch += 1
        watch_id = f"w{self._next_watch}"
        self.watches[watch_id] = (channel, callback)
        return watch_id

    def unwatch(self, watch_id: str) -> None:
        self.watches.pop(watch_id, None)


def _boom(_message: Message) -> None:
    raise RuntimeError("subscriber boom")


def _watch_driver(backend: _WatchingMemoryBackend, channel: str) -> Callable[[Message], None]:
    """Return the callback the Bus registered with the backend for *channel*."""
    for watched_channel, callback in backend.watches.values():
        if watched_channel == channel:
            return callback
    raise AssertionError(f"no watch registered for {channel!r}")


class TestPublishIsolation:
    def test_failing_subscriber_does_not_abort_publish(self) -> None:
        bus = Bus(backend=MemoryBackend())
        received: list[Message] = []
        bus.subscribe("ch", _boom)
        bus.subscribe("ch", received.append)

        msg_id = bus.publish("ch", "sender", "text", "hello")

        assert [m.id for m in bus.query("ch")] == [msg_id]
        assert [m.id for m in received] == [msg_id]

    def test_failing_subscriber_delivered_last(self) -> None:
        bus = Bus(backend=MemoryBackend())
        received: list[Message] = []
        bus.subscribe("ch", received.append)
        bus.subscribe("ch", _boom)

        msg_id = bus.publish("ch", "sender", "text", "hello")

        assert [m.id for m in received] == [msg_id]


class TestWatchIsolation:
    def test_throwing_callback_does_not_kill_watch_delivery(self) -> None:
        backend = _WatchingMemoryBackend()
        bus = Bus(backend=backend)
        received: list[Message] = []
        bus.subscribe("ch", _boom)
        bus.subscribe("ch", received.append)

        driver = _watch_driver(backend, "ch")
        msg = Message(
            id="01a00000-0000-7000-8000-000000000000",
            channel="ch",
            sender="other-process",
            msg_type="text",
            payload="from another process",
            timestamp="2026-01-01T00:00:00+00:00",
        )

        driver(msg)  # must not raise
        driver(msg)  # the watch thread keeps delivering later messages

        assert [m.id for m in received] == [msg.id, msg.id]
        assert len(backend.watches) == 1
