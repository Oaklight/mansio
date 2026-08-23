"""Tests for mansio message bus and modular components."""

import json
import multiprocessing as mp
import multiprocessing.synchronize
import sqlite3
import time

import pytest

from mansio import (
    Bus,
    JSONSerializer,
    MemoryBackend,
    Message,
    SQLiteBackend,
    SQLiteBus,
)


def _concurrent_open_worker(
    db_path: str,
    idx: int,
    start_evt: multiprocessing.synchronize.Event,
    q: mp.Queue,  # type: ignore[type-arg]
) -> None:
    """Module-level worker for test_concurrent_cold_start.

    Waits on start_evt so all workers race the WAL switch at the same
    instant. Must be top-level so it can be pickled by spawn.
    """
    start_evt.wait(timeout=10)
    try:
        try:
            b = SQLiteBackend(db_path)
        except Exception as e:  # noqa: BLE001
            q.put((idx, f"init: {type(e).__name__}: {e}"))
            return
        b.store(
            Message(
                id=f"id-{idx:04d}",
                channel="ch",
                sender=f"writer-{idx}",
                msg_type="text",
                payload=str(idx),
                timestamp="2026-01-01T00:00:00+00:00",
            )
        )
        b.close()
        q.put((idx, None))
    except Exception as e:  # noqa: BLE001
        q.put((idx, f"{type(e).__name__}: {e}"))


# ──────────────────────────────────────────────
# Message dataclass
# ──────────────────────────────────────────────


class TestMessage:
    """Tests for Message dataclass."""

    def test_create_message(self):
        msg = Message(
            id="test-id",
            channel="test-ch",
            sender="agent-a",
            msg_type="text",
            payload="hello",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        assert msg.id == "test-id"
        assert msg.channel == "test-ch"
        assert msg.sender == "agent-a"
        assert msg.msg_type == "text"
        assert msg.payload == "hello"
        assert msg.metadata is None

    def test_message_is_frozen(self):
        msg = Message(
            id="test-id",
            channel="ch",
            sender="a",
            msg_type="text",
            payload="x",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(AttributeError):
            msg.id = "new-id"

    def test_payload_json(self):
        data = {"key": "value", "count": 42}
        msg = Message(
            id="test-id",
            channel="ch",
            sender="a",
            msg_type="json",
            payload=json.dumps(data),
            timestamp="2026-01-01T00:00:00+00:00",
        )
        assert msg.payload_json() == data

    def test_payload_json_invalid(self):
        msg = Message(
            id="test-id",
            channel="ch",
            sender="a",
            msg_type="text",
            payload="not json",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(json.JSONDecodeError):
            msg.payload_json()

    def test_metadata(self):
        meta = {"priority": "high", "tags": ["urgent"]}
        msg = Message(
            id="test-id",
            channel="ch",
            sender="a",
            msg_type="text",
            payload="x",
            timestamp="2026-01-01T00:00:00+00:00",
            metadata=meta,
        )
        assert msg.metadata == meta


# ──────────────────────────────────────────────
# JSONSerializer
# ──────────────────────────────────────────────


class TestJSONSerializer:
    """Tests for JSONSerializer."""

    def test_encode_decode_roundtrip(self):
        ser = JSONSerializer()
        data = {"key": "value", "nested": {"a": 1}}
        encoded = ser.encode(data)
        assert isinstance(encoded, str)
        assert ser.decode(encoded) == data

    def test_encode_unicode(self):
        ser = JSONSerializer()
        data = {"name": "mansio 广场"}
        encoded = ser.encode(data)
        assert "mansio 广场" in encoded  # ensure_ascii=False


# ──────────────────────────────────────────────
# MemoryBackend
# ──────────────────────────────────────────────


class TestMemoryBackend:
    """Tests for MemoryBackend."""

    def _make_msg(self, channel="ch", msg_id="id-1", payload="x"):
        return Message(
            id=msg_id,
            channel=channel,
            sender="a",
            msg_type="text",
            payload=payload,
            timestamp="2026-01-01T00:00:00+00:00",
        )

    def test_store_and_query(self):
        backend = MemoryBackend()
        msg = self._make_msg()
        backend.store(msg)
        result = backend.query("ch")
        assert len(result) == 1
        assert result[0].payload == "x"

    def test_query_empty(self):
        backend = MemoryBackend()
        assert backend.query("nonexistent") == []

    def test_query_with_after(self):
        backend = MemoryBackend()
        backend.store(self._make_msg(msg_id="aaa"))
        backend.store(self._make_msg(msg_id="bbb"))
        backend.store(self._make_msg(msg_id="ccc"))
        result = backend.query("ch", after="aaa")
        assert len(result) == 2
        assert result[0].id == "bbb"

    def test_query_with_limit(self):
        backend = MemoryBackend()
        for i in range(5):
            backend.store(self._make_msg(msg_id=f"id-{i:03d}"))
        result = backend.query("ch", limit=2)
        assert len(result) == 2

    def test_list_channels(self):
        backend = MemoryBackend()
        backend.store(self._make_msg(channel="beta"))
        backend.store(self._make_msg(channel="alpha"))
        assert backend.list_channels() == ["alpha", "beta"]

    def test_close_clears(self):
        backend = MemoryBackend()
        backend.store(self._make_msg())
        backend.close()
        assert backend.query("ch") == []

    def test_repr(self):
        assert repr(MemoryBackend()) == "MemoryBackend()"


# ──────────────────────────────────────────────
# SQLiteBackend
# ──────────────────────────────────────────────


class TestSQLiteBackend:
    """Tests for SQLiteBackend."""

    def _make_msg(self, channel="ch", msg_id="id-1", payload="x", metadata=None):
        return Message(
            id=msg_id,
            channel=channel,
            sender="a",
            msg_type="text",
            payload=payload,
            timestamp="2026-01-01T00:00:00+00:00",
            metadata=metadata,
        )

    def test_store_and_query(self):
        backend = SQLiteBackend()
        backend.store(self._make_msg())
        result = backend.query("ch")
        assert len(result) == 1
        assert result[0].payload == "x"
        backend.close()

    def test_metadata_roundtrip(self):
        backend = SQLiteBackend()
        meta = {"tags": ["a", "b"]}
        backend.store(self._make_msg(metadata=meta))
        result = backend.query("ch")
        assert result[0].metadata == meta
        backend.close()

    def test_persistence(self, tmp_path):
        db = tmp_path / "test.db"
        backend = SQLiteBackend(db)
        backend.store(self._make_msg())
        backend.close()

        backend2 = SQLiteBackend(db)
        result = backend2.query("ch")
        assert len(result) == 1
        backend2.close()

    def test_repr(self):
        backend = SQLiteBackend()
        assert repr(backend) == "SQLiteBackend(':memory:')"
        backend.close()

    def test_concurrent_cold_start(self, tmp_path):
        """Multiple processes opening the same DB concurrently must not
        fail with 'database is locked'.

        Regression test: switching to WAL journal_mode requires an
        exclusive lock. Without an explicit busy_timeout, the default
        0ms causes concurrent cold-starts to fail immediately. The
        backend must set busy_timeout before any other PRAGMA so the
        WAL switch waits for the lock instead of crashing.
        """
        db = tmp_path / "concurrent.db"

        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        start_evt = ctx.Event()
        n_workers = 8
        procs = [
            ctx.Process(
                target=_concurrent_open_worker,
                args=(str(db), i, start_evt, q),
            )
            for i in range(n_workers)
        ]
        for p in procs:
            p.start()
        # Give workers time to spawn + import + reach wait()
        time.sleep(0.5)
        start_evt.set()
        for p in procs:
            p.join(timeout=15)
            assert not p.is_alive(), "worker hung"

        results = [q.get(timeout=1) for _ in procs]
        errors = [(i, e) for i, e in results if e]
        assert not errors, f"concurrent cold-start failed: {errors}"

        # All writes landed
        verify = SQLiteBackend(db)
        msgs = verify.query("ch", limit=100)
        verify.close()
        assert len(msgs) == n_workers


# ──────────────────────────────────────────────
# Bus (composable)
# ──────────────────────────────────────────────


class TestBusWithMemoryBackend:
    """Tests for Bus with MemoryBackend."""

    def test_publish_and_poll(self):
        with Bus(backend=MemoryBackend()) as bus:
            bus.publish("ch", "agent-a", "text", "msg1")
            bus.publish("ch", "agent-b", "text", "msg2")
            msgs = bus.query("ch")
            assert len(msgs) == 2
            assert msgs[0].payload == "msg1"
            assert msgs[1].payload == "msg2"

    def test_subscribe(self):
        received = []
        with Bus(backend=MemoryBackend()) as bus:
            bus.subscribe("ch", lambda msg: received.append(msg))
            bus.publish("ch", "a", "text", "hello")
            assert len(received) == 1
            assert received[0].payload == "hello"

    def test_backend_property(self):
        mem = MemoryBackend()
        bus = Bus(backend=mem)
        assert bus.backend is mem
        bus.close()

    def test_serializer_property(self):
        bus = Bus(backend=MemoryBackend())
        assert isinstance(bus.serializer, JSONSerializer)
        bus.close()

    def test_repr(self):
        bus = Bus(backend=MemoryBackend())
        assert repr(bus) == "Bus(backend=MemoryBackend())"
        bus.close()


# ──────────────────────────────────────────────
# SQLiteBus (convenience subclass)
# ──────────────────────────────────────────────


class TestSQLiteBusInMemory:
    """Tests for SQLiteBus using in-memory database."""

    def test_publish_returns_id(self):
        with SQLiteBus() as bus:
            msg_id = bus.publish("ch", "agent-a", "text", "hello")
            assert isinstance(msg_id, str)
            assert len(msg_id) > 0

    def test_publish_and_poll(self):
        with SQLiteBus() as bus:
            bus.publish("ch", "agent-a", "text", "msg1")
            bus.publish("ch", "agent-b", "text", "msg2")
            msgs = bus.query("ch")
            assert len(msgs) == 2
            assert msgs[0].payload == "msg1"
            assert msgs[1].payload == "msg2"
            assert msgs[0].sender == "agent-a"
            assert msgs[1].sender == "agent-b"

    def test_poll_empty_channel(self):
        with SQLiteBus() as bus:
            assert bus.query("nonexistent") == []

    def test_poll_with_cursor(self):
        with SQLiteBus() as bus:
            id1 = bus.publish("ch", "a", "text", "msg1")
            bus.publish("ch", "a", "text", "msg2")
            bus.publish("ch", "a", "text", "msg3")
            msgs = bus.query("ch", after=id1)
            assert len(msgs) == 2
            assert msgs[0].payload == "msg2"
            assert msgs[1].payload == "msg3"

    def test_poll_with_limit(self):
        with SQLiteBus() as bus:
            for i in range(10):
                bus.publish("ch", "a", "text", f"msg{i}")
            msgs = bus.query("ch", limit=3)
            assert len(msgs) == 3

    def test_poll_channel_isolation(self):
        with SQLiteBus() as bus:
            bus.publish("ch1", "a", "text", "in-ch1")
            bus.publish("ch2", "a", "text", "in-ch2")
            assert bus.query("ch1")[0].payload == "in-ch1"
            assert bus.query("ch2")[0].payload == "in-ch2"

    def test_poll_chronological_order(self):
        with SQLiteBus() as bus:
            for i in range(5):
                bus.publish("ch", "a", "text", f"msg{i}")
            payloads = [m.payload for m in bus.query("ch")]
            assert payloads == ["msg0", "msg1", "msg2", "msg3", "msg4"]

    def test_subscribe_receives_messages(self):
        received = []
        with SQLiteBus() as bus:
            bus.subscribe("ch", lambda msg: received.append(msg))
            bus.publish("ch", "a", "text", "hello")
            assert len(received) == 1
            assert received[0].payload == "hello"

    def test_subscribe_channel_isolation(self):
        received = []
        with SQLiteBus() as bus:
            bus.subscribe("ch1", lambda msg: received.append(msg))
            bus.publish("ch2", "a", "text", "wrong channel")
            assert len(received) == 0

    def test_unsubscribe(self):
        received = []
        with SQLiteBus() as bus:
            sub_id = bus.subscribe("ch", lambda msg: received.append(msg))
            bus.publish("ch", "a", "text", "msg1")
            bus.unsubscribe(sub_id)
            bus.publish("ch", "a", "text", "msg2")
            assert len(received) == 1

    def test_multiple_subscribers(self):
        received_a = []
        received_b = []
        with SQLiteBus() as bus:
            bus.subscribe("ch", lambda msg: received_a.append(msg))
            bus.subscribe("ch", lambda msg: received_b.append(msg))
            bus.publish("ch", "sender", "text", "broadcast")
            assert len(received_a) == 1
            assert len(received_b) == 1

    def test_channels_empty(self):
        with SQLiteBus() as bus:
            assert bus.channels() == []

    def test_channels_lists_all(self):
        with SQLiteBus() as bus:
            bus.publish("beta", "a", "text", "x")
            bus.publish("alpha", "a", "text", "x")
            bus.publish("gamma", "a", "text", "x")
            assert bus.channels() == ["alpha", "beta", "gamma"]

    def test_metadata_roundtrip(self):
        with SQLiteBus() as bus:
            meta = {"priority": "high", "tags": ["a", "b"]}
            bus.publish("ch", "a", "text", "x", metadata=meta)
            assert bus.query("ch")[0].metadata == meta

    def test_context_manager_closes(self):
        bus = SQLiteBus()
        with bus:
            bus.publish("ch", "a", "text", "x")
        with pytest.raises(sqlite3.ProgrammingError):
            bus.publish("ch", "a", "text", "y")

    def test_repr(self):
        bus = SQLiteBus()
        assert "SQLiteBus" in repr(bus)
        assert ":memory:" in repr(bus)
        bus.close()


class TestSQLiteBusFileBacked:
    """Tests for SQLiteBus with file-backed database."""

    def test_persistence_across_instances(self, tmp_path):
        db_file = tmp_path / "test.db"
        with SQLiteBus(db_file) as bus:
            bus.publish("ch", "a", "text", "persistent-msg")

        with SQLiteBus(db_file) as bus:
            msgs = bus.query("ch")
            assert len(msgs) == 1
            assert msgs[0].payload == "persistent-msg"

    def test_repr_with_path(self, tmp_path):
        db_file = tmp_path / "test.db"
        bus = SQLiteBus(db_file)
        assert str(db_file) in repr(bus)
        bus.close()


# ──────────────────────────────────────────────
# Backward compatibility aliases
# ──────────────────────────────────────────────


class TestBackwardCompatAliases:
    """Verify deprecated aliases still work."""

    def test_sqlite_storage_alias(self):
        from mansio import SQLiteStorage

        assert SQLiteStorage is SQLiteBackend

    def test_memory_storage_alias(self):
        from mansio import MemoryStorage

        assert MemoryStorage is MemoryBackend

    def test_storage_backend_alias(self):
        from mansio import StorageBackend
        from mansio.protocols import Backend

        assert StorageBackend is Backend
