"""Tests for SSE slow-consumer drop notification.

Verifies that when the SSE event queue fills up and events are
dropped, a warning comment is sent to the client.

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

from mansio import Bus, MemoryBackend
from mansio.frontends.http import (
    _drain_drop_counter,
    _format_sse_event,
    _setup_sse_subscriptions,
    _sse_event_generator,
)


class TestDropCounter(unittest.TestCase):
    """Verify the drop counter is incremented on queue overflow."""

    def test_drop_counter_increments_on_overflow(self):
        """When queue is full, drop_counter should increment."""
        bus = Bus(MemoryBackend())

        loop = asyncio.new_event_loop()

        async def _run():
            q, sub_ids, drop_lock, drop_counter = _setup_sse_subscriptions(bus, ["test-ch"], None)
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
    """Verify that the SSE generator emits a warning comment after drops."""

    def test_generator_emits_drop_warning(self):
        """After drops, generator should yield a warning SSE comment."""
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
                q,
                [],
                bus,
                ["test-ch"],
                None,
                None,
                drop_lock,
                drop_counter,
            ):
                events.append(chunk)

            # Should have: connected comment, the event, drop warning
            event_text = "".join(events)
            self.assertIn(": connected", event_text)
            self.assertIn("msg-1", event_text)
            self.assertIn(": warning: 5 event(s) dropped (slow consumer)", event_text)

            # Counter should be reset
            with drop_lock:
                self.assertEqual(drop_counter[0], 0)

        loop.run_until_complete(_run())
        loop.close()

    def test_generator_no_warning_without_drops(self):
        """When no drops, no warning comment should appear."""
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
                q,
                [],
                bus,
                ["test-ch"],
                None,
                None,
                drop_lock,
                drop_counter,
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
        self.assertIn("7 event(s) dropped", result)
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


if __name__ == "__main__":
    unittest.main()
