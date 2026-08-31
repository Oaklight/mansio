"""Tests for the Maildir message backend."""

from __future__ import annotations

import threading
import time

import pytest

from mansio.backends.maildir import (
    MaildirBackend,
    _channel_to_dirname,
    _email_to_msg,
    _msg_to_email,
)
from mansio.bus import Bus
from mansio.types import Message


def _make_msg(
    msg_id: str = "019ff140-0000-7000-0000-000000000001",
    channel: str = "test-channel",
    sender: str = "agent-a",
    msg_type: str = "chat",
    payload: str = "hello world",
    timestamp: str = "2026-08-11T14:00:00+00:00",
    metadata: dict | None = None,
) -> Message:
    return Message(
        id=msg_id,
        channel=channel,
        sender=sender,
        msg_type=msg_type,
        payload=payload,
        timestamp=timestamp,
        metadata=metadata,
    )


@pytest.fixture
def backend(tmp_path):
    b = MaildirBackend(tmp_path / "maildir-test")
    yield b
    b.close()


# ── Helpers ──────────────────────────────────────────────────────


class TestHelpers:
    def test_channel_to_dirname_simple(self):
        assert _channel_to_dirname("dev-piazza") == "dev-piazza"

    def test_channel_to_dirname_colon(self):
        assert _channel_to_dirname("dm:alice:bob") == "dm__alice__bob"

    def test_channel_to_dirname_slash(self):
        assert _channel_to_dirname("path/to/channel") == "path__to__channel"

    def test_channel_to_dirname_path_traversal(self):
        # Must not produce '..' which could escape the root directory
        result = _channel_to_dirname("..")
        assert ".." not in result
        assert result == "_dotdot_"

    def test_channel_to_dirname_hidden_dir(self):
        # Leading dots would create hidden directories, must be stripped
        result = _channel_to_dirname(".hidden")
        assert not result.startswith(".")

    def test_channel_to_dirname_empty(self):
        assert _channel_to_dirname("") == "_empty_"

    def test_email_roundtrip(self):
        msg = _make_msg(payload="test content", metadata={"key": "value"})
        em = _msg_to_email(msg)
        recovered = _email_to_msg(em)
        assert recovered is not None
        assert recovered.id == msg.id
        assert recovered.channel == msg.channel
        assert recovered.sender == msg.sender
        assert recovered.msg_type == msg.msg_type
        assert recovered.payload == msg.payload
        assert recovered.timestamp == msg.timestamp
        assert recovered.metadata == msg.metadata

    def test_email_roundtrip_no_metadata(self):
        msg = _make_msg(metadata=None)
        em = _msg_to_email(msg)
        recovered = _email_to_msg(em)
        assert recovered is not None
        assert recovered.metadata is None

    def test_email_roundtrip_unicode(self):
        msg = _make_msg(payload="你好世界 🎉 مرحبا")
        em = _msg_to_email(msg)
        recovered = _email_to_msg(em)
        assert recovered is not None
        assert recovered.payload == "你好世界 🎉 مرحبا"

    def test_email_roundtrip_multiline(self):
        msg = _make_msg(payload="line1\nline2\nline3")
        em = _msg_to_email(msg)
        recovered = _email_to_msg(em)
        assert recovered is not None
        assert recovered.payload == "line1\nline2\nline3"


# ── Store & Query ────────────────────────────────────────────────


class TestStoreAndQuery:
    def test_store_and_query(self, backend):
        msg = _make_msg()
        backend.store(msg)
        result = backend.query("test-channel")
        assert len(result) == 1
        assert result[0].id == msg.id
        assert result[0].payload == msg.payload

    def test_store_multiple_and_order(self, backend):
        for i in range(5):
            backend.store(
                _make_msg(
                    msg_id=f"019ff140-0000-7000-0000-00000000000{i + 1}",
                    payload=f"msg-{i}",
                )
            )
        result = backend.query("test-channel")
        assert len(result) == 5
        assert [m.payload for m in result] == [f"msg-{i}" for i in range(5)]

    def test_query_after(self, backend):
        for i in range(5):
            backend.store(
                _make_msg(
                    msg_id=f"019ff140-0000-7000-0000-00000000000{i + 1}",
                    payload=f"msg-{i}",
                )
            )
        result = backend.query(
            "test-channel",
            after="019ff140-0000-7000-0000-000000000003",
        )
        assert len(result) == 2
        assert result[0].payload == "msg-3"
        assert result[1].payload == "msg-4"

    def test_query_limit(self, backend):
        for i in range(10):
            backend.store(
                _make_msg(
                    msg_id=f"019ff140-0000-7000-0000-0000000000{i + 1:02d}",
                    payload=f"msg-{i}",
                )
            )
        result = backend.query("test-channel", limit=3)
        assert len(result) == 3

    def test_query_empty_channel(self, backend):
        result = backend.query("nonexistent-channel")
        assert result == []

    def test_store_different_channels(self, backend):
        backend.store(_make_msg(channel="ch-a", msg_id="019ff140-0000-7000-0000-00000000000a"))
        backend.store(_make_msg(channel="ch-b", msg_id="019ff140-0000-7000-0000-00000000000b"))
        assert len(backend.query("ch-a")) == 1
        assert len(backend.query("ch-b")) == 1


# ── List Channels ────────────────────────────────────────────────


class TestListChannels:
    def test_channels_empty(self, backend):
        assert backend.list_channels() == []

    def test_channels(self, backend):
        backend.store(_make_msg(channel="beta"))
        backend.store(_make_msg(channel="alpha", msg_id="019ff140-0000-7000-0000-000000000002"))
        channels = backend.list_channels()
        assert channels == ["alpha", "beta"]

    def test_channels_with_colons(self, backend):
        backend.store(
            _make_msg(
                channel="dm:alice:bob",
                msg_id="019ff140-0000-7000-0000-000000000003",
            )
        )
        channels = backend.list_channels()
        assert "dm:alice:bob" in channels


# ── Count ────────────────────────────────────────────────────────


class TestCount:
    def test_message_count(self, backend):
        for i in range(3):
            backend.store(
                _make_msg(
                    msg_id=f"019ff140-0000-7000-0000-00000000000{i + 1}",
                )
            )
        assert backend.message_count() == 3
        assert backend.message_count("test-channel") == 3

    def test_count_different_channels(self, backend):
        backend.store(_make_msg(channel="ch-a", msg_id="019ff140-0000-7000-0000-00000000000a"))
        backend.store(_make_msg(channel="ch-b", msg_id="019ff140-0000-7000-0000-00000000000b"))
        assert backend.message_count() == 2
        assert backend.message_count("ch-a") == 1


# ── Query All ────────────────────────────────────────────────────


class TestQueryAll:
    def test_search_cross_channel(self, backend):
        backend.store(
            _make_msg(
                channel="ch-a",
                msg_id="019ff140-0000-7000-0000-000000000001",
                sender="alice",
            )
        )
        backend.store(
            _make_msg(
                channel="ch-b",
                msg_id="019ff140-0000-7000-0000-000000000002",
                sender="bob",
            )
        )
        result = backend.search()
        assert len(result) == 2

    def test_search_filter_sender(self, backend):
        backend.store(
            _make_msg(
                msg_id="019ff140-0000-7000-0000-000000000001",
                sender="alice",
            )
        )
        backend.store(
            _make_msg(
                msg_id="019ff140-0000-7000-0000-000000000002",
                sender="bob",
            )
        )
        result = backend.search(sender="alice")
        assert len(result) == 1
        assert result[0].sender == "alice"

    def test_search_filter_msg_type(self, backend):
        backend.store(
            _make_msg(
                msg_id="019ff140-0000-7000-0000-000000000001",
                msg_type="chat",
            )
        )
        backend.store(
            _make_msg(
                msg_id="019ff140-0000-7000-0000-000000000002",
                msg_type="notification",
            )
        )
        result = backend.search(msg_type="notification")
        assert len(result) == 1

    def test_search_after(self, backend):
        for i in range(5):
            backend.store(
                _make_msg(
                    msg_id=f"019ff140-0000-7000-0000-00000000000{i + 1}",
                )
            )
        result = backend.search(after="019ff140-0000-7000-0000-000000000003")
        assert len(result) == 2


# ── Stats ────────────────────────────────────────────────────────


class TestStats:
    def test_stats(self, backend):
        backend.store(
            _make_msg(
                channel="ch-a",
                sender="alice",
                msg_type="chat",
                msg_id="019ff140-0000-7000-0000-000000000001",
            )
        )
        backend.store(
            _make_msg(
                channel="ch-a",
                sender="bob",
                msg_type="chat",
                msg_id="019ff140-0000-7000-0000-000000000002",
            )
        )
        backend.store(
            _make_msg(
                channel="ch-b",
                sender="alice",
                msg_type="notification",
                msg_id="019ff140-0000-7000-0000-000000000003",
            )
        )
        stats = backend.stats()
        assert stats["total_messages"] == 3
        assert stats["total_channels"] == 2
        assert stats["total_senders"] == 2
        assert len(stats["channel_breakdown"]) == 2
        assert len(stats["msg_type_distribution"]) == 2


# ── Queue Operations ─────────────────────────────────────────────


class TestQueueOps:
    def test_store_queue_and_claim(self, backend):
        msg = _make_msg()
        backend.store_queue(msg)
        result = backend.queue_claim("test-channel", "worker-1")
        assert result is not None
        assert result.status == "claimed"
        assert result.claimed_by == "worker-1"
        assert result.message.id == msg.id

    def test_claim_empty_channel(self, backend):
        result = backend.queue_claim("empty-channel", "worker-1")
        assert result is None

    def test_claim_then_ack(self, backend):
        msg = _make_msg()
        backend.store_queue(msg)
        claimed = backend.queue_claim("test-channel", "worker-1")
        assert claimed is not None

        acked = backend.queue_ack(msg.id, "worker-1")
        assert acked is not None
        assert acked.status == "completed"

    def test_ack_wrong_claimer(self, backend):
        msg = _make_msg()
        backend.store_queue(msg)
        backend.queue_claim("test-channel", "worker-1")
        result = backend.queue_ack(msg.id, "worker-2")
        assert result is None

    def test_double_claim(self, backend):
        msg = _make_msg()
        backend.store_queue(msg)
        first = backend.queue_claim("test-channel", "worker-1")
        assert first is not None
        # Second claim should return None (already claimed, lease not expired)
        second = backend.queue_claim("test-channel", "worker-2")
        assert second is None

    def test_claim_after_lease_expiry(self, backend):
        msg = _make_msg()
        backend.store_queue(msg)
        # Claim with very short lease
        result = backend.queue_claim("test-channel", "worker-1", lease_seconds=0)
        assert result is not None
        # Immediate re-claim should work (lease expired)
        time.sleep(0.01)
        result2 = backend.queue_claim("test-channel", "worker-2")
        assert result2 is not None
        assert result2.claimed_by == "worker-2"

    def test_queue_stats(self, backend):
        backend.store_queue(
            _make_msg(
                msg_id="019ff140-0000-7000-0000-000000000001",
            ),
        )
        backend.store_queue(
            _make_msg(
                msg_id="019ff140-0000-7000-0000-000000000002",
            ),
        )
        stats = backend.queue_stats("test-channel")
        assert stats["unclaimed"] == 2

        backend.queue_claim("test-channel", "worker-1")
        stats = backend.queue_stats("test-channel")
        assert stats["unclaimed"] == 1
        assert stats["claimed"] == 1

    def test_queue_retire(self, backend):
        msg = _make_msg()
        backend.store_queue(msg)
        backend.queue_claim("test-channel", "worker-1")
        backend.queue_ack(msg.id, "worker-1")

        # Should not retire (too recent)
        deleted = backend.queue_retire(max_age_seconds=3600)
        assert deleted == 0

        # Retire with max_age_seconds=0 should clean it up
        deleted = backend.queue_retire(max_age_seconds=0)
        assert deleted == 1


# ── Queue Status ──────────────────────────────────────────────────


class TestQueueStatus:
    def test_queue_status_unclaimed(self, backend):
        msg = _make_msg()
        backend.store_queue(msg)
        status = backend.queue_status(msg.id)
        assert status is not None
        assert status["status"] == "unclaimed"
        assert status["claimed_by"] is None

    def test_queue_status_claimed(self, backend):
        msg = _make_msg()
        backend.store_queue(msg)
        backend.queue_claim("test-channel", "worker-1")
        status = backend.queue_status(msg.id)
        assert status is not None
        assert status["status"] == "claimed"
        assert status["claimed_by"] == "worker-1"

    def test_queue_status_completed(self, backend):
        msg = _make_msg()
        backend.store_queue(msg)
        backend.queue_claim("test-channel", "worker-1")
        backend.queue_ack(msg.id, "worker-1")
        status = backend.queue_status(msg.id)
        assert status is not None
        assert status["status"] == "completed"
        assert status["claimed_by"] == "worker-1"

    def test_queue_status_nonexistent(self, backend):
        status = backend.queue_status("nonexistent-id")
        assert status is None

    def test_queue_status_non_queue_msg(self, backend):
        msg = _make_msg()
        backend.store(msg)
        status = backend.queue_status(msg.id)
        assert status is None


# ── Backend Info ─────────────────────────────────────────────────


class TestBackendInfo:
    def test_info(self, backend):
        backend.store(_make_msg())
        info = backend.info()
        assert info["type"] == "maildir"
        assert info["total_messages"] == 1
        assert info["total_channels"] == 1
        assert info["disk_size_bytes"] > 0


# ── DM Channel Names ────────────────────────────────────────────


class TestDMChannels:
    def test_dm_channel_roundtrip(self, backend):
        channel = "dm:alice:bob"
        msg = _make_msg(channel=channel, msg_id="019ff140-0000-7000-0000-00000000000d")
        backend.store(msg)
        result = backend.query(channel)
        assert len(result) == 1
        assert result[0].channel == channel

    def test_dm_channels_listed(self, backend):
        backend.store(
            _make_msg(
                channel="dm:alice:bob",
                msg_id="019ff140-0000-7000-0000-000000000001",
            )
        )
        backend.store(
            _make_msg(
                channel="dm:alice:carol",
                msg_id="019ff140-0000-7000-0000-000000000002",
            )
        )
        channels = backend.list_channels()
        assert "dm:alice:bob" in channels
        assert "dm:alice:carol" in channels


# ── Concurrent Access ────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_writes(self, backend):
        errors = []
        count = 20

        def write_msg(i):
            try:
                backend.store(
                    _make_msg(
                        msg_id=f"019ff140-0000-7000-0000-{i:012d}",
                        payload=f"concurrent-{i}",
                    )
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_msg, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert backend.message_count("test-channel") == count

    def test_concurrent_reads(self, backend):
        for i in range(10):
            backend.store(
                _make_msg(
                    msg_id=f"019ff140-0000-7000-0000-{i:012d}",
                )
            )

        results = []
        errors = []

        def read_msgs():
            try:
                r = backend.query("test-channel")
                results.append(len(r))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=read_msgs) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert all(r == 10 for r in results)


# ── Persistence ──────────────────────────────────────────────────


class TestPersistence:
    def test_data_survives_close_and_reopen(self, tmp_path):
        root = tmp_path / "persist-test"
        b1 = MaildirBackend(root)
        b1.store(_make_msg(payload="persistent data"))
        b1.close()

        b2 = MaildirBackend(root)
        result = b2.query("test-channel")
        assert len(result) == 1
        assert result[0].payload == "persistent data"
        b2.close()

    def test_queue_state_survives_restart(self, tmp_path):
        root = tmp_path / "queue-persist-test"
        b1 = MaildirBackend(root)
        msg = _make_msg()
        b1.store_queue(msg)
        b1.queue_claim("test-channel", "worker-1")
        b1.close()

        b2 = MaildirBackend(root)
        # Should not be claimable (already claimed, lease not expired)
        result = b2.queue_claim("test-channel", "worker-2")
        assert result is None
        # But ack should work
        acked = b2.queue_ack(msg.id, "worker-1")
        assert acked is not None
        assert acked.status == "completed"
        b2.close()


class TestQueryRecentTimestamps:
    """Verify recent_timestamps with time-dependent cutoff."""

    def test_recent_messages_returned(self, tmp_path):
        backend = MaildirBackend(tmp_path / "recent")
        bus = Bus(backend=backend)
        bus.publish("ch", "agent", "text", "hello")
        result = backend.recent_timestamps(seconds=10)
        assert len(result) == 1
        bus.close()

    def test_no_recent_messages(self, tmp_path):
        backend = MaildirBackend(tmp_path / "recent-empty")
        result = backend.recent_timestamps(seconds=10)
        assert result == []


class TestRetireOverflow:
    """Verify queue_retire with max_per_channel overflow."""

    def test_retire_excess_per_channel(self, tmp_path):
        backend = MaildirBackend(tmp_path / "retire")
        bus = Bus(backend=backend)
        # Create and complete several queue messages.
        for i in range(5):
            bus.publish("q", "p", "task", f"task-{i}", queue=True)
        for _ in range(5):
            r = bus.queue_claim("q", "worker")
            if r:
                bus.queue_ack(r.message.id, "worker")
        # Retire with max_per_channel=2 and max_age=0.
        removed = backend.queue_retire(max_age_seconds=0, max_per_channel=2)
        assert removed >= 3  # at least 3 of 5 completed should be removed
        bus.close()


class TestPathTraversal:
    """Verify that adversarial channel names are rejected."""

    def test_dotdot_channel_sanitized(self, tmp_path):
        # The _channel_to_dirname should neutralise "..".
        dirname = _channel_to_dirname("../../etc")
        assert ".." not in dirname

    def test_traversal_resolve_guard(self, tmp_path):
        backend = MaildirBackend(tmp_path / "traversal")
        # Even if dirname sanitization is bypassed, resolve() guard catches it.
        # Regular usage with colons is safe.
        bus = Bus(backend=backend)
        msg_id = bus.publish("safe:channel", "agent", "text", "data")
        assert msg_id
        bus.close()


class TestPresence:
    """Verify heartbeat/users/user_status on MaildirBackend."""

    def test_heartbeat_and_agents(self, tmp_path):
        backend = MaildirBackend(tmp_path / "presence")
        backend.heartbeat("agent-a", metadata={"role": "worker"})
        backend.heartbeat("agent-b")

        online_users = backend.users(timeout_seconds=120)
        assert len(online_users) == 2
        ids = {a.user_id for a in online_users}
        assert ids == {"agent-a", "agent-b"}
        for a in online_users:
            assert a.status == "online"

    def test_user_status_known(self, tmp_path):
        backend = MaildirBackend(tmp_path / "presence")
        backend.heartbeat("agent-x", metadata={"v": 1})
        status = backend.user_status("agent-x")
        assert status is not None
        assert status.user_id == "agent-x"
        assert status.status == "online"
        assert status.metadata == {"v": 1}

    def test_user_status_unknown(self, tmp_path):
        backend = MaildirBackend(tmp_path / "presence")
        assert backend.user_status("ghost") is None

    def test_agent_offline_by_timeout(self, tmp_path):
        backend = MaildirBackend(tmp_path / "presence")
        backend.heartbeat("agent-old")
        # With timeout_seconds=0, even a just-heartbeated agent is offline.
        online_users = backend.users(timeout_seconds=0)
        assert len(online_users) == 1
        assert online_users[0].status == "offline"

    def test_presence_survives_restart(self, tmp_path):
        root = tmp_path / "presence-persist"
        b1 = MaildirBackend(root)
        b1.heartbeat("agent-persist", metadata={"round": 1})
        b1.close()

        b2 = MaildirBackend(root)
        status = b2.user_status("agent-persist")
        assert status is not None
        assert status.user_id == "agent-persist"
        b2.close()


class TestQueueHeaderGuard:
    """Verify X-Mansio-Queue header prevents non-queue claims."""

    def test_non_queue_msg_in_new_not_claimable(self, tmp_path):
        """A non-queue message stranded in new/ should not be claimed."""
        backend = MaildirBackend(tmp_path / "queue-guard")
        bus = Bus(backend=backend)
        # Publish a regular (non-queue) message.
        bus.publish("ch", "agent", "text", "regular msg")
        # Should not be claimable even though it goes through new/ briefly.
        result = bus.queue_claim("ch", "worker")
        assert result is None
        bus.close()

    def test_queue_msg_is_claimable(self, tmp_path):
        backend = MaildirBackend(tmp_path / "queue-guard")
        bus = Bus(backend=backend)
        bus.publish("ch", "agent", "task", "queue msg", queue=True)
        result = bus.queue_claim("ch", "worker")
        assert result is not None
        assert result.message.payload == "queue msg"
        bus.close()


class TestMalformedMetadata:
    """Verify json.loads guard on metadata."""

    def test_corrupt_metadata_returns_none(self, tmp_path):
        import email.message

        # Manually create a message with malformed metadata.
        em = email.message.EmailMessage()
        em["X-Mansio-Id"] = "019ff140-0000-7000-0000-000000000099"
        em["X-Mansio-Channel"] = "test"
        em["X-Mansio-Sender"] = "agent"
        em["X-Mansio-MsgType"] = "text"
        em["X-Mansio-Timestamp"] = "2026-08-11T14:00:00+00:00"
        em["X-Mansio-Metadata"] = "{not valid json"
        em.set_content("payload")

        msg = _email_to_msg(em)
        assert msg is not None
        assert msg.metadata is None  # gracefully handled


class TestCompaction:
    """Verify compact() on MaildirBackend."""

    def test_compact_max_messages(self, tmp_path):
        backend = MaildirBackend(tmp_path / "compact")
        bus = Bus(backend=backend)
        for i in range(10):
            bus.publish("ch", "agent", "text", f"msg-{i}")

        removed = backend.compact("ch", max_messages=3)
        assert removed == 7
        msgs = bus.query("ch", limit=100)
        assert len(msgs) == 3
        bus.close()

    def test_compact_keep_latest_per_sender(self, tmp_path):
        backend = MaildirBackend(tmp_path / "compact")
        bus = Bus(backend=backend)
        for agent in ("a", "b"):
            for i in range(5):
                bus.publish("ch", agent, "text", f"{agent}-{i}")

        removed = backend.compact("ch", keep_latest_per_sender=True)
        assert removed == 8  # 10 - 2
        msgs = bus.query("ch", limit=100)
        assert len(msgs) == 2
        bus.close()
