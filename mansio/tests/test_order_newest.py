"""Tests for order='newest' support (GitHub issue #54).

Verifies that channel_read(limit=N) returns the newest N messages
(in chronological order) and that backward compatibility is preserved.
"""

from __future__ import annotations

import pytest

from mansio import Bus, MemoryBackend
from mansio.backends.memory import MemoryBackend as MemoryBackendDirect
from mansio.backends.sqlite import SQLiteBackend

# ── Helpers ───────────────────────────────────────────────────


def _publish_n(bus: Bus, n: int, channel: str = "ch") -> list[str]:
    """Publish n messages and return their IDs."""
    ids = []
    for i in range(n):
        msg_id = bus.publish(channel, "agent", "text", f"msg-{i}")
        ids.append(msg_id)
    return ids


# ── MemoryBackend ─────────────────────────────────────────────


class TestMemoryBackendOrder:
    """Test order parameter on MemoryBackend.query()."""

    def test_newest_returns_last_n(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, order="newest")
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-2", "msg-3", "msg-4"]

    def test_oldest_returns_first_n(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, order="oldest")
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-0", "msg-1", "msg-2"]

    def test_default_order_is_oldest(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3)
        assert [m.payload for m in result] == ["msg-0", "msg-1", "msg-2"]

    def test_newest_with_after_cursor(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        ids = _publish_n(bus, 6)

        # After the 2nd message, get newest 2 → should be msg-4, msg-5
        result = backend.query("ch", after=ids[1], limit=2, order="newest")
        assert len(result) == 2
        assert [m.payload for m in result] == ["msg-4", "msg-5"]

    def test_newest_chronological_order(self):
        """Newest should still return messages in ascending ID order."""
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, order="newest")
        assert result[0].id < result[1].id < result[2].id

    def test_newest_limit_larger_than_total(self):
        backend = MemoryBackendDirect()
        bus = Bus(backend=backend)
        _publish_n(bus, 3)

        result = backend.query("ch", limit=10, order="newest")
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-0", "msg-1", "msg-2"]


# ── SQLiteBackend ─────────────────────────────────────────────


class TestSQLiteBackendOrder:
    """Test order parameter on SQLiteBackend.query()."""

    def test_newest_returns_last_n(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, order="newest")
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-2", "msg-3", "msg-4"]
        backend.close()

    def test_oldest_returns_first_n(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, order="oldest")
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-0", "msg-1", "msg-2"]
        backend.close()

    def test_default_order_is_oldest(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3)
        assert [m.payload for m in result] == ["msg-0", "msg-1", "msg-2"]
        backend.close()

    def test_newest_with_after_cursor(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        ids = _publish_n(bus, 6)

        result = backend.query("ch", after=ids[1], limit=2, order="newest")
        assert len(result) == 2
        assert [m.payload for m in result] == ["msg-4", "msg-5"]
        backend.close()

    def test_newest_chronological_order(self):
        """Newest should still return messages in ascending ID order."""
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        _publish_n(bus, 5)

        result = backend.query("ch", limit=3, order="newest")
        assert result[0].id < result[1].id < result[2].id
        backend.close()

    def test_newest_limit_larger_than_total(self):
        backend = SQLiteBackend()
        bus = Bus(backend=backend)
        _publish_n(bus, 3)

        result = backend.query("ch", limit=10, order="newest")
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-0", "msg-1", "msg-2"]
        backend.close()


# ── Bus.query() ───────────────────────────────────────────────


class TestBusQueryOrder:
    """Test order parameter on Bus.query()."""

    def test_query_newest(self):
        with Bus(backend=MemoryBackend()) as bus:
            _publish_n(bus, 5)
            msgs = bus.query("ch", limit=3, order="newest")
            assert [m.payload for m in msgs] == ["msg-2", "msg-3", "msg-4"]

    def test_query_oldest_default(self):
        with Bus(backend=MemoryBackend()) as bus:
            _publish_n(bus, 5)
            msgs = bus.query("ch", limit=3)
            assert [m.payload for m in msgs] == ["msg-0", "msg-1", "msg-2"]

    def test_query_newest_with_after(self):
        with Bus(backend=MemoryBackend()) as bus:
            ids = _publish_n(bus, 6)
            msgs = bus.query("ch", after=ids[1], limit=2, order="newest")
            assert [m.payload for m in msgs] == ["msg-4", "msg-5"]


# ── HTTP Frontend ─────────────────────────────────────────────


class TestHttpFrontendOrder:
    """Test order query parameter via HTTP."""

    def test_query_order_newest(self, server_url):
        from mansio import HttpTransport

        transport = HttpTransport(server_url)
        for i in range(5):
            transport.publish("test-order", "agent", "text", f"msg-{i}")

        result = transport.query("test-order", limit=3, order="newest")
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-2", "msg-3", "msg-4"]
        transport.close()

    def test_query_order_oldest_default(self, server_url):
        from mansio import HttpTransport

        transport = HttpTransport(server_url)
        for i in range(5):
            transport.publish("test-order-old", "agent", "text", f"msg-{i}")

        result = transport.query("test-order-old", limit=3)
        assert len(result) == 3
        assert [m.payload for m in result] == ["msg-0", "msg-1", "msg-2"]
        transport.close()

    def test_query_order_invalid(self, server_url):
        from mansio import HttpTransport, MansioAPIError

        transport = HttpTransport(server_url)
        transport.publish("test-order-inv", "agent", "text", "msg")

        with pytest.raises(MansioAPIError, match="400"):
            transport.query("test-order-inv", order="invalid")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        transport.close()


# ── MansioClient ──────────────────────────────────────────────


class TestMansioClientOrder:
    """Test MansioClient.channel_read() order parameter."""

    def test_channel_read_defaults_to_newest(self, server_url):
        from mansio import MansioClient

        with MansioClient(server_url, "test-agent") as client:
            for i in range(5):
                client.channel_send("test-read-newest", f"msg-{i}")

            msgs = client.channel_read("test-read-newest", limit=3)
            assert [m.payload for m in msgs] == ["msg-2", "msg-3", "msg-4"]

    def test_channel_read_oldest_explicit(self, server_url):
        from mansio import MansioClient

        with MansioClient(server_url, "test-agent") as client:
            for i in range(5):
                client.channel_send("test-read-oldest", f"msg-{i}")

            msgs = client.channel_read("test-read-oldest", limit=3, order="oldest")
            assert [m.payload for m in msgs] == ["msg-0", "msg-1", "msg-2"]

    def test_channel_poll_uses_oldest(self, server_url):
        """channel_poll() should use oldest order (cursor-based forward pagination)."""
        from mansio import MansioClient

        with MansioClient(server_url, "test-agent") as client:
            for i in range(5):
                client.channel_send("test-poll-order", f"msg-{i}")

            msgs = client.channel_poll("test-poll-order")
            assert [m.payload for m in msgs] == [
                "msg-0",
                "msg-1",
                "msg-2",
                "msg-3",
                "msg-4",
            ]
