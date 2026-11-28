"""Tests for SSE slow-consumer drop notification.

Verifies that when the SSE event queue fills up and events are
dropped, a typed ``event: warning`` frame is sent to the client.

Closes #24 (item 3).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "mansio-client", "src"))

from mansio_core import Bus, MemoryBackend
from mansio_core.authz import ChannelAuthorizer
from mansio_core.operations import OperationContext
from mansio_core.principal import Principal
from mansio_web.adapters.routes.sse import (
    _SSE_REPLAY_LIMIT,
    _drain_drop_counter,
    _format_sse_event,
    _replay_missed_events,
    _setup_sse_subscriptions,
    _sse_event_generator,
    _SSESession,
)


def _operations(bus: Bus) -> OperationContext:
    """Operation context for a test bus."""
    return OperationContext(
        bus=bus,
        authz=ChannelAuthorizer(bus),
        token_store=None,
        allow_unauthenticated=False,
    )


async def _collect_replay(session: _SSESession) -> list[str]:
    return [chunk async for chunk in _replay_missed_events(session)]


class _TimeoutThenStopQueue:
    """Queue stand-in: first ``get()`` times out, the next signals shutdown."""

    def __init__(self) -> None:
        self._timed_out = False

    async def get(self) -> None:
        if not self._timed_out:
            self._timed_out = True
            raise asyncio.TimeoutError
        return None


class TestDropCounter(unittest.TestCase):
    """Verify the drop counter is incremented on queue overflow."""

    def test_drop_counter_increments_on_overflow(self):
        """When queue is full, drop_counter should increment."""
        bus = Bus(MemoryBackend())

        loop = asyncio.new_event_loop()

        async def _run():
            q, sub_ids, drop_lock, drop_counter = await _setup_sse_subscriptions(
                _operations(bus), ["test-ch"], Principal.supertoken()
            )
            # Fill the queue to capacity (256)
            for i in range(256):
                q.put_nowait(f"event-{i}")

            # Now publish a message — should trigger drop
            bus.publish("test-ch", "agent-a", "text", "overflow-msg")

            # Give the callback time to fire
            await asyncio.sleep(0.05)

            with drop_lock:
                drops = drop_counter[0]

            self.assertGreaterEqual(drops, 1, "Expected at least 1 drop")

            # Cleanup
            for sid in sub_ids:
                bus.unsubscribe(sid)

        loop.run_until_complete(_run())
        loop.close()


class TestDropWarningInGenerator(unittest.TestCase):
    """Verify that the SSE generator emits a typed warning after drops."""

    def test_generator_emits_drop_warning(self):
        """After drops, generator should yield an ``event: warning`` frame."""
        bus = Bus(MemoryBackend())

        loop = asyncio.new_event_loop()

        async def _run():
            q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=256)
            drop_lock = threading.Lock()
            drop_counter = [5]  # simulate 5 drops already

            # Put one event in the queue
            msg_data = json.dumps(
                {
                    "channel": "test-ch",
                    "message": {
                        "id": "msg-1",
                        "channel": "test-ch",
                        "sender": "agent-a",
                        "msg_type": "text",
                        "payload": "hello",
                        "timestamp": "2026-01-01T00:00:00Z",
                    },
                }
            )
            q.put_nowait(msg_data)
            # Then signal end
            q.put_nowait(None)

            events = []
            async for chunk in _sse_event_generator(
                _SSESession(
                    queue=q,
                    sub_ids=[],
                    operations=_operations(bus),
                    channels=["test-ch"],
                    principal=Principal.supertoken(),
                    last_event_id=None,
                    drop_lock=drop_lock,
                    drop_counter=drop_counter,
                )
            ):
                events.append(chunk)

            # Should have: connected comment, the event, the drop warning
            event_text = "".join(events)
            self.assertIn(": connected", event_text)
            self.assertIn("msg-1", event_text)
            self.assertIn(
                'event: warning\ndata: {"kind": "dropped", "count": 5, "channel": null}\n\n',
                event_text,
            )

            # Counter should be reset
            with drop_lock:
                self.assertEqual(drop_counter[0], 0)

        loop.run_until_complete(_run())
        loop.close()

    def test_generator_no_warning_without_drops(self):
        """When no drops, no warning frame should appear."""
        bus = Bus(MemoryBackend())

        loop = asyncio.new_event_loop()

        async def _run():
            q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=256)
            drop_lock = threading.Lock()
            drop_counter = [0]  # no drops

            msg_data = json.dumps(
                {
                    "channel": "test-ch",
                    "message": {
                        "id": "msg-1",
                        "channel": "test-ch",
                        "sender": "agent-a",
                        "msg_type": "text",
                        "payload": "hello",
                        "timestamp": "2026-01-01T00:00:00Z",
                    },
                }
            )
            q.put_nowait(msg_data)
            q.put_nowait(None)

            events = []
            async for chunk in _sse_event_generator(
                _SSESession(
                    queue=q,
                    sub_ids=[],
                    operations=_operations(bus),
                    channels=["test-ch"],
                    principal=Principal.supertoken(),
                    last_event_id=None,
                    drop_lock=drop_lock,
                    drop_counter=drop_counter,
                )
            ):
                events.append(chunk)

            event_text = "".join(events)
            self.assertNotIn("warning", event_text)
            self.assertNotIn("dropped", event_text)

        loop.run_until_complete(_run())
        loop.close()


class TestDrainDropCounter(unittest.TestCase):
    """Verify _drain_drop_counter helper."""

    def test_returns_warning_and_resets(self):
        lock = threading.Lock()
        counter = [7]
        result = _drain_drop_counter(lock, counter)
        self.assertIn("event: warning", result)
        self.assertIn('"kind": "dropped"', result)
        self.assertIn('"count": 7', result)
        self.assertIn('"channel": null', result)
        self.assertEqual(counter[0], 0)

    def test_returns_empty_when_no_drops(self):
        lock = threading.Lock()
        counter = [0]
        result = _drain_drop_counter(lock, counter)
        self.assertEqual(result, "")

    def test_returns_empty_when_none(self):
        result = _drain_drop_counter(None, None)
        self.assertEqual(result, "")


class TestFormatSseEvent(unittest.TestCase):
    """Verify _format_sse_event helper."""

    def test_with_id(self):
        result = _format_sse_event("hello", "evt-1")
        self.assertEqual(result, "id: evt-1\ndata: hello\n\n")

    def test_without_id(self):
        result = _format_sse_event("hello", "")
        self.assertEqual(result, "data: hello\n\n")


class TestQuietStreamDropWarning(unittest.TestCase):
    """A stream with no traffic still reports drops on the keepalive tick."""

    def test_drop_warning_emitted_on_keepalive_tick(self):
        bus = Bus(MemoryBackend())
        loop = asyncio.new_event_loop()
        drop_lock = threading.Lock()
        drop_counter = [3]

        async def _run():
            session = _SSESession(
                queue=_TimeoutThenStopQueue(),
                sub_ids=[],
                operations=_operations(bus),
                channels=["quiet-ch"],
                principal=Principal.supertoken(),
                drop_lock=drop_lock,
                drop_counter=drop_counter,
            )
            return [chunk async for chunk in _sse_event_generator(session)]

        try:
            events = loop.run_until_complete(_run())
        finally:
            loop.close()

        text = "".join(events)
        self.assertIn(": keepalive", text)
        self.assertIn(
            'event: warning\ndata: {"kind": "dropped", "count": 3, "channel": null}\n\n', text
        )
        self.assertEqual(drop_counter[0], 0, "Counter should reset after the warning")

    def test_no_warning_on_quiet_stream_without_drops(self):
        bus = Bus(MemoryBackend())
        loop = asyncio.new_event_loop()
        drop_lock = threading.Lock()
        drop_counter = [0]

        async def _run():
            session = _SSESession(
                queue=_TimeoutThenStopQueue(),
                sub_ids=[],
                operations=_operations(bus),
                channels=["quiet-ch"],
                principal=Principal.supertoken(),
                drop_lock=drop_lock,
                drop_counter=drop_counter,
            )
            return [chunk async for chunk in _sse_event_generator(session)]

        try:
            events = loop.run_until_complete(_run())
        finally:
            loop.close()

        text = "".join(events)
        self.assertIn(": keepalive", text)
        self.assertNotIn("warning", text)


class TestReplayTruncationWarning(unittest.TestCase):
    """A replay that fills the limit is flagged as possibly truncated."""

    def _replay(self, bus, channel: str, last_event_id: str) -> list[str]:
        loop = asyncio.new_event_loop()
        session = _SSESession(
            queue=asyncio.Queue(),
            sub_ids=[],
            operations=_operations(bus),
            channels=[channel],
            principal=Principal.supertoken(),
            last_event_id=last_event_id,
        )
        try:
            return loop.run_until_complete(_collect_replay(session))
        finally:
            loop.close()

    def test_warning_when_replay_hits_limit(self):
        bus = Bus(MemoryBackend())
        ids = [
            bus.publish("trunc-ch", "agent-a", "chat", f"m{i}")
            for i in range(_SSE_REPLAY_LIMIT + 1)
        ]

        events = self._replay(bus, "trunc-ch", ids[0])

        self.assertEqual(len(events), _SSE_REPLAY_LIMIT + 1)
        self.assertEqual(
            events[0],
            'event: warning\ndata: {"kind": "truncated", "count": 500, "channel": "trunc-ch"}\n\n',
        )
        self.assertIn("m1", events[1])

    def test_no_warning_for_short_replay(self):
        bus = Bus(MemoryBackend())
        first = bus.publish("short-ch", "agent-a", "chat", "m0")
        bus.publish("short-ch", "agent-a", "chat", "m1")

        events = self._replay(bus, "short-ch", first)

        self.assertEqual(len(events), 1)
        self.assertNotIn("warning", "".join(events))


if __name__ == "__main__":
    unittest.main()
