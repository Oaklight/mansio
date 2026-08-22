"""Tests for NATS JetStream backend.

These tests require a running NATS server with JetStream enabled.
Skip if nats-py is not installed or server is unavailable.

Run NATS for testing::

    docker run -d --name nats-test -p 4222:4222 nats:latest -js

Or with nats-server directly::

    nats-server --jetstream
"""

from __future__ import annotations

import os
import socket
import time
import urllib.parse

import pytest

nats_py = pytest.importorskip("nats")  # noqa: E402

from piazza.backends.nats import NATSBackend  # noqa: E402
from piazza.types import Message  # noqa: E402

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")

# Unique stream name per test run to avoid collisions
_RUN_ID = f"test_{int(time.time() * 1000)}"


def _make_backend(stream_suffix: str = "") -> NATSBackend:
    """Create a test backend with a unique stream name."""
    stream_name = f"PIAZZA_TEST_{_RUN_ID}{stream_suffix}"
    return NATSBackend(
        url=NATS_URL,
        stream_name=stream_name,
        subject_prefix=f"piazza_test_{_RUN_ID}{stream_suffix}",
        storage="memory",  # Use memory storage for test speed
    )


def _is_nats_available() -> bool:
    """Check if NATS server is reachable via TCP socket probe.

    Uses a plain socket connection instead of ``asyncio.run()`` to avoid
    creating/destroying an event loop at module collection time, which
    can interfere with async test fixtures.
    """
    parsed = urllib.parse.urlparse(NATS_URL)
    host = parsed.hostname or "localhost"
    port = parsed.port or 4222
    try:
        sock = socket.create_connection((host, port), timeout=2)
        sock.close()
        return True
    except OSError:
        return False


# Skip all tests if NATS unavailable
pytestmark = pytest.mark.skipif(
    not _is_nats_available(),
    reason=f"NATS server not available at {NATS_URL}",
)


@pytest.fixture
def backend():
    """Create a connected NATS backend for testing."""
    b = _make_backend()
    b.connect()
    yield b
    # Cleanup: purge the stream then close
    try:

        async def _purge():
            assert b._js is not None
            await b._js.purge_stream(b._stream_name)

        b._run_async(_purge())
    except Exception:
        pass
    b.close()


def _make_msg(
    channel: str = "test-channel",
    sender: str = "agent-a",
    msg_type: str = "text",
    payload: str = "hello",
    msg_id: str | None = None,
    metadata: dict | None = None,
) -> Message:
    """Create a test Message."""
    from datetime import datetime, timezone

    return Message(
        id=msg_id or f"msg-{time.time_ns()}",
        channel=channel,
        sender=sender,
        msg_type=msg_type,
        payload=payload,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata=metadata,
    )


class TestNATSBackendStore:
    """Tests for store operation."""

    def test_store_single_message(self, backend: NATSBackend):
        msg = _make_msg(payload="test store")
        backend.store(msg)
        results = backend.query("test-channel")
        assert len(results) == 1
        assert results[0].payload == "test store"

    def test_store_preserves_all_fields(self, backend: NATSBackend):
        msg = _make_msg(
            channel="ch1",
            sender="bot-x",
            msg_type="context_sync",
            payload='{"key": "val"}',
            metadata={"priority": "high"},
        )
        backend.store(msg)
        results = backend.query("ch1")
        assert len(results) == 1
        r = results[0]
        assert r.channel == "ch1"
        assert r.sender == "bot-x"
        assert r.msg_type == "context_sync"
        assert r.payload == '{"key": "val"}'
        assert r.metadata == {"priority": "high"}

    def test_store_multiple_channels(self, backend: NATSBackend):
        backend.store(_make_msg(channel="ch-a", payload="a"))
        backend.store(_make_msg(channel="ch-b", payload="b"))
        assert len(backend.query("ch-a")) == 1
        assert len(backend.query("ch-b")) == 1


class TestNATSBackendQuery:
    """Tests for query operation."""

    def test_query_empty_channel(self, backend: NATSBackend):
        results = backend.query("nonexistent")
        assert results == []

    def test_query_with_after_cursor(self, backend: NATSBackend):
        msg1 = _make_msg(msg_id="00001")
        msg2 = _make_msg(msg_id="00002")
        msg3 = _make_msg(msg_id="00003")
        backend.store(msg1)
        backend.store(msg2)
        backend.store(msg3)

        results = backend.query("test-channel", after="00001")
        assert len(results) == 2
        assert results[0].id == "00002"
        assert results[1].id == "00003"

    def test_query_with_limit(self, backend: NATSBackend):
        for i in range(5):
            backend.store(_make_msg(msg_id=f"msg-{i:05d}"))

        results = backend.query("test-channel", limit=3)
        assert len(results) == 3

    def test_query_chronological_order(self, backend: NATSBackend):
        ids = [f"msg-{i:05d}" for i in range(5)]
        for mid in ids:
            backend.store(_make_msg(msg_id=mid))

        results = backend.query("test-channel")
        result_ids = [m.id for m in results]
        assert result_ids == ids


class TestNATSBackendListChannels:
    """Tests for list_channels operation."""

    def test_list_channels_empty(self, backend: NATSBackend):
        channels = backend.list_channels()
        assert channels == []

    def test_list_channels_multiple(self, backend: NATSBackend):
        backend.store(_make_msg(channel="alpha"))
        backend.store(_make_msg(channel="beta"))
        backend.store(_make_msg(channel="gamma"))
        channels = backend.list_channels()
        assert channels == ["alpha", "beta", "gamma"]


class TestNATSBackendQueryAll:
    """Tests for query_all with filters."""

    def test_query_all_filter_by_sender(self, backend: NATSBackend):
        backend.store(_make_msg(sender="alice", msg_id="m1"))
        backend.store(_make_msg(sender="bob", msg_id="m2"))
        backend.store(_make_msg(sender="alice", msg_id="m3"))

        results = backend.query_all(sender="alice")
        assert len(results) == 2
        assert all(m.sender == "alice" for m in results)

    def test_query_all_filter_by_msg_type(self, backend: NATSBackend):
        backend.store(_make_msg(msg_type="text", msg_id="m1"))
        backend.store(_make_msg(msg_type="notification", msg_id="m2"))

        results = backend.query_all(msg_type="notification")
        assert len(results) == 1
        assert results[0].msg_type == "notification"

    def test_query_all_filter_by_channel(self, backend: NATSBackend):
        backend.store(_make_msg(channel="ch1", msg_id="m1"))
        backend.store(_make_msg(channel="ch2", msg_id="m2"))

        results = backend.query_all(channel="ch1")
        assert len(results) == 1
        assert results[0].channel == "ch1"


class TestNATSBackendCountMessages:
    """Tests for count_messages."""

    def test_count_all(self, backend: NATSBackend):
        backend.store(_make_msg(channel="c1", msg_id="m1"))
        backend.store(_make_msg(channel="c2", msg_id="m2"))
        backend.store(_make_msg(channel="c1", msg_id="m3"))
        assert backend.count_messages() == 3

    def test_count_by_channel(self, backend: NATSBackend):
        backend.store(_make_msg(channel="c1", msg_id="m1"))
        backend.store(_make_msg(channel="c2", msg_id="m2"))
        backend.store(_make_msg(channel="c1", msg_id="m3"))
        assert backend.count_messages(channel="c1") == 2
        assert backend.count_messages(channel="c2") == 1


class TestNATSBackendInfo:
    """Tests for get_backend_info."""

    def test_backend_info_connected(self, backend: NATSBackend):
        info = backend.get_backend_info()
        assert info["type"] == "nats"
        assert info["connected"] is True
        assert info["stream"] == backend._stream_name

    def test_backend_info_disconnected(self):
        b = _make_backend("_disconnected")
        info = b.get_backend_info()
        assert info["type"] == "nats"
        assert info["connected"] is False


class TestNATSBackendRepr:
    """Tests for repr."""

    def test_repr(self, backend: NATSBackend):
        r = repr(backend)
        assert "NATSBackend" in r
        assert NATS_URL in r


class TestNATSBackendUnicode:
    """Tests for unicode/special character handling."""

    def test_unicode_payload(self, backend: NATSBackend):
        msg = _make_msg(payload="你好世界 🌍 こんにちは")
        backend.store(msg)
        results = backend.query("test-channel")
        assert len(results) == 1
        assert results[0].payload == "你好世界 🌍 こんにちは"

    def test_unicode_channel_name(self, backend: NATSBackend):
        msg = _make_msg(channel="频道-test")
        backend.store(msg)
        results = backend.query("频道-test")
        assert len(results) == 1

    def test_json_payload(self, backend: NATSBackend):
        payload = '{"nested": {"key": "value"}, "list": [1, 2, 3]}'
        msg = _make_msg(payload=payload)
        backend.store(msg)
        results = backend.query("test-channel")
        assert results[0].payload == payload


class TestNATSBackendChannelEncoding:
    """Tests for lossless channel name encoding."""

    def test_underscore_channel_roundtrip(self, backend: NATSBackend):
        """Underscore channels must not be mangled."""
        msg = _make_msg(channel="dev_piazza")
        backend.store(msg)
        results = backend.query("dev_piazza")
        assert len(results) == 1
        assert results[0].channel == "dev_piazza"

    def test_dotted_channel_roundtrip(self, backend: NATSBackend):
        """Dots must be encoded to avoid NATS subject hierarchy."""
        msg = _make_msg(channel="config.v2.beta")
        backend.store(msg)
        results = backend.query("config.v2.beta")
        assert len(results) == 1
        assert results[0].channel == "config.v2.beta"

    def test_space_channel_roundtrip(self, backend: NATSBackend):
        msg = _make_msg(channel="my channel")
        backend.store(msg)
        results = backend.query("my channel")
        assert len(results) == 1
        assert results[0].channel == "my channel"

    def test_dm_colon_channel_roundtrip(self, backend: NATSBackend):
        msg = _make_msg(channel="dm:alice:bob")
        backend.store(msg)
        results = backend.query("dm:alice:bob")
        assert len(results) == 1
        assert results[0].channel == "dm:alice:bob"


class TestNATSBackendQueueNotImplemented:
    """Queue operations should raise NotImplementedError."""

    def test_claim_not_implemented(self, backend: NATSBackend):
        with pytest.raises(NotImplementedError):
            backend.claim("test-channel", "worker-1")

    def test_ack_not_implemented(self, backend: NATSBackend):
        with pytest.raises(NotImplementedError):
            backend.ack("msg-1", "worker-1")

    def test_queue_stats_not_implemented(self, backend: NATSBackend):
        with pytest.raises(NotImplementedError):
            backend.get_queue_stats()

    def test_retire_not_implemented(self, backend: NATSBackend):
        with pytest.raises(NotImplementedError):
            backend.retire_completed()


class TestNATSBackendPresence:
    """Tests for presence (heartbeat / roster)."""

    def test_heartbeat_records_agent(self, backend: NATSBackend):
        backend.heartbeat("agent-a")
        agents = backend.agents()
        assert len(agents) == 1
        assert agents[0].agent_id == "agent-a"
        assert agents[0].status == "online"

    def test_heartbeat_with_metadata(self, backend: NATSBackend):
        backend.heartbeat("agent-a", metadata={"name": "Alice"})
        result = backend.agent_status("agent-a")
        assert result is not None
        assert result.metadata == {"name": "Alice"}

    def test_agent_unknown(self, backend: NATSBackend):
        assert backend.agent_status("nonexistent") is None

    def test_agent_offline_by_timeout(self, backend: NATSBackend):
        backend.heartbeat("agent-a")
        result = backend.agent_status("agent-a", timeout_seconds=0)
        assert result is not None
        assert result.status == "offline"

    def test_agents_sorted(self, backend: NATSBackend):
        backend.heartbeat("charlie")
        backend.heartbeat("alice")
        backend.heartbeat("bob")
        agents = backend.agents()
        assert [a.agent_id for a in agents] == ["alice", "bob", "charlie"]


class TestNATSBackendGetStats:
    """Tests for get_stats."""

    def test_stats_empty_stream(self, backend: NATSBackend):
        stats = backend.get_stats()
        assert stats["total_messages"] == 0
        assert stats["total_senders"] == 0
        assert stats["channel_breakdown"] == []
        assert stats["msg_type_distribution"] == []

    def test_stats_with_messages(self, backend: NATSBackend):
        backend.store(_make_msg(channel="ch1", sender="alice", msg_type="chat", msg_id="s1"))
        backend.store(_make_msg(channel="ch1", sender="bob", msg_type="chat", msg_id="s2"))
        backend.store(_make_msg(channel="ch2", sender="alice", msg_type="note", msg_id="s3"))

        stats = backend.get_stats()
        assert stats["total_messages"] == 3
        assert stats["total_senders"] == 2
        assert len(stats["channel_breakdown"]) == 2

        # ch1 has 2 msgs, ch2 has 1 — ch1 should be first (sorted by count desc)
        assert stats["channel_breakdown"][0]["channel"] == "ch1"
        assert stats["channel_breakdown"][0]["message_count"] == 2
        assert stats["channel_breakdown"][0]["sender_count"] == 2
        assert stats["channel_breakdown"][1]["channel"] == "ch2"
        assert stats["channel_breakdown"][1]["message_count"] == 1

        # msg_type_distribution
        type_map = {d["msg_type"]: d["count"] for d in stats["msg_type_distribution"]}
        assert type_map["chat"] == 2
        assert type_map["note"] == 1


class TestNATSBackendQueryRecentTimestamps:
    """Tests for query_recent_timestamps."""

    def test_recent_timestamps_returns_current(self, backend: NATSBackend):
        """Messages stored just now should appear in a 60s window."""
        backend.store(_make_msg(msg_id="rt1"))
        backend.store(_make_msg(msg_id="rt2"))
        time.sleep(0.3)  # allow NATS to persist

        timestamps = backend.query_recent_timestamps(seconds=60)
        assert len(timestamps) == 2
        # Timestamps should be sorted ascending
        assert timestamps == sorted(timestamps)

    def test_recent_timestamps_empty(self, backend: NATSBackend):
        """Empty stream returns no timestamps."""
        timestamps = backend.query_recent_timestamps(seconds=60)
        assert timestamps == []
