"""Tests for queue (competing consumer) semantics."""

from __future__ import annotations

import threading

import pytest

from mansio import Bus, ClaimResult, MemoryBackend, Message, SQLiteBackend


@pytest.fixture(params=["memory", "sqlite"])
def bus(request, tmp_path):
    if request.param == "memory":
        b = Bus(backend=MemoryBackend())
    else:
        b = Bus(backend=SQLiteBackend(tmp_path / "test.db"))
    yield b
    b.close()


class TestQueuePublish:
    def test_publish_queue_returns_id(self, bus):
        msg_id = bus.publish("jobs", "admin", "task", "do stuff", queue=True)
        assert isinstance(msg_id, str)
        assert len(msg_id) > 0

    def test_regular_publish_not_claimable(self, bus):
        bus.publish("jobs", "admin", "task", "regular msg")
        result = bus.queue_claim("jobs", "worker-1")
        assert result is None

    def test_queue_publish_is_claimable(self, bus):
        bus.publish("jobs", "admin", "task", "queue msg", queue=True)
        result = bus.queue_claim("jobs", "worker-1")
        assert result is not None
        assert result.message.payload == "queue msg"


class TestClaim:
    def test_claim_returns_oldest_unclaimed(self, bus):
        bus.publish("jobs", "admin", "task", "first", queue=True)
        bus.publish("jobs", "admin", "task", "second", queue=True)
        r1 = bus.queue_claim("jobs", "worker-1")
        assert r1 is not None
        assert r1.message.payload == "first"
        r2 = bus.queue_claim("jobs", "worker-2")
        assert r2 is not None
        assert r2.message.payload == "second"

    def test_claim_empty_channel_returns_none(self, bus):
        assert bus.queue_claim("empty-channel", "worker-1") is None

    def test_claim_all_claimed_returns_none(self, bus):
        bus.publish("jobs", "admin", "task", "only one", queue=True)
        bus.queue_claim("jobs", "worker-1")
        assert bus.queue_claim("jobs", "worker-2") is None

    def test_claim_result_fields(self, bus):
        bus.publish("jobs", "admin", "task", "payload", queue=True)
        result = bus.queue_claim("jobs", "worker-1")
        assert isinstance(result, ClaimResult)
        assert isinstance(result.message, Message)
        assert result.status == "claimed"
        assert result.claimed_by == "worker-1"
        assert result.claimed_at  # non-empty ISO string

    def test_claim_result_includes_lease_until(self, bus):
        bus.publish("jobs", "admin", "task", "payload", queue=True)
        result = bus.queue_claim("jobs", "worker-1", lease_seconds=60)
        assert result is not None
        assert result.lease_until is not None
        # lease_until should be a valid ISO timestamp after claimed_at
        from datetime import datetime

        claimed = datetime.fromisoformat(result.claimed_at)
        lease = datetime.fromisoformat(result.lease_until)
        delta = (lease - claimed).total_seconds()
        assert 59 <= delta <= 61  # ~60 seconds lease

    def test_ack_result_has_no_lease_until(self, bus):
        bus.publish("jobs", "admin", "task", "payload", queue=True)
        result = bus.queue_claim("jobs", "worker-1")
        assert result is not None
        acked = bus.queue_ack(result.message.id, "worker-1")
        assert acked is not None
        assert acked.lease_until is None  # ack doesn't set lease

    def test_claim_atomic_two_threads(self, bus):
        for i in range(10):
            bus.publish("jobs", "admin", "task", f"task-{i}", queue=True)

        results: list[ClaimResult | None] = []
        lock = threading.Lock()

        def claim_all(worker_id):
            while True:
                r = bus.queue_claim("jobs", worker_id)
                if r is None:
                    break
                with lock:
                    results.append(r)

        t1 = threading.Thread(target=claim_all, args=("worker-a",))
        t2 = threading.Thread(target=claim_all, args=("worker-b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(results) == 10
        ids = [r.message.id for r in results if r is not None]
        assert len(set(ids)) == 10


class TestAck:
    def test_ack_marks_completed(self, bus):
        bus.publish("jobs", "admin", "task", "do it", queue=True)
        claimed = bus.queue_claim("jobs", "worker-1")
        assert claimed is not None
        acked = bus.queue_ack(claimed.message.id, "worker-1")
        assert acked is not None
        assert acked.status == "completed"

    def test_ack_wrong_agent_returns_none(self, bus):
        bus.publish("jobs", "admin", "task", "do it", queue=True)
        claimed = bus.queue_claim("jobs", "worker-1")
        assert claimed is not None
        assert bus.queue_ack(claimed.message.id, "worker-2") is None

    def test_ack_unclaimed_returns_none(self, bus):
        bus.publish("jobs", "admin", "task", "unclaimed", queue=True)
        msgs = bus.query("jobs")
        assert bus.queue_ack(msgs[0].id, "worker-1") is None

    def test_ack_nonexistent_returns_none(self, bus):
        assert bus.queue_ack("nonexistent-id", "worker-1") is None

    def test_double_ack_returns_none(self, bus):
        bus.publish("jobs", "admin", "task", "do it", queue=True)
        claimed = bus.queue_claim("jobs", "worker-1")
        assert claimed is not None
        bus.queue_ack(claimed.message.id, "worker-1")
        assert bus.queue_ack(claimed.message.id, "worker-1") is None


class TestQueryBackwardCompat:
    def test_query_returns_all_regardless_of_status(self, bus):
        bus.publish("jobs", "admin", "text", "regular")
        bus.publish("jobs", "admin", "task", "queued", queue=True)
        bus.publish("jobs", "admin", "task", "claimed-then-done", queue=True)

        bus.queue_claim("jobs", "worker-1")
        claimed = bus.queue_claim("jobs", "worker-1")
        if claimed:
            bus.queue_ack(claimed.message.id, "worker-1")

        msgs = bus.query("jobs")
        assert len(msgs) == 3


class TestQueueStats:
    def test_stats_counts(self, bus):
        bus.publish("jobs", "admin", "task", "t1", queue=True)
        bus.publish("jobs", "admin", "task", "t2", queue=True)
        bus.publish("jobs", "admin", "task", "t3", queue=True)

        claimed = bus.queue_claim("jobs", "worker-1")
        assert claimed is not None
        bus.queue_ack(claimed.message.id, "worker-1")
        bus.queue_claim("jobs", "worker-2")

        stats = bus.backend.queue_stats("jobs")
        assert stats["unclaimed"] == 1
        assert stats["claimed"] == 1
        assert stats["completed"] == 1

    def test_stats_global(self, bus):
        bus.publish("ch-a", "admin", "task", "a", queue=True)
        bus.publish("ch-b", "admin", "task", "b", queue=True)
        stats = bus.backend.queue_stats()
        assert stats["unclaimed"] == 2


class TestRetireCompleted:
    def test_retire_by_ttl(self, bus):
        bus.publish("jobs", "admin", "task", "old", queue=True)
        claimed = bus.queue_claim("jobs", "worker-1")
        assert claimed is not None
        bus.queue_ack(claimed.message.id, "worker-1")

        deleted = bus.backend.queue_retire(max_age_seconds=0)
        assert deleted == 1

        stats = bus.backend.queue_stats("jobs")
        assert stats["completed"] == 0

    def test_retire_by_count(self, bus):
        for i in range(5):
            bus.publish("jobs", "admin", "task", f"t-{i}", queue=True)
        for _ in range(5):
            c = bus.queue_claim("jobs", "worker-1")
            assert c is not None
            bus.queue_ack(c.message.id, "worker-1")

        deleted = bus.backend.queue_retire(max_age_seconds=999999, max_per_channel=2)
        assert deleted == 3

        stats = bus.backend.queue_stats("jobs")
        assert stats["completed"] == 2

    def test_retire_doesnt_touch_unclaimed(self, bus):
        bus.publish("jobs", "admin", "task", "pending", queue=True)
        deleted = bus.backend.queue_retire(max_age_seconds=0)
        assert deleted == 0
        stats = bus.backend.queue_stats("jobs")
        assert stats["unclaimed"] == 1


class TestLeaseTimeout:
    def test_expired_lease_reclaimable(self, bus):
        bus.publish("jobs", "admin", "task", "leased", queue=True)
        r1 = bus.queue_claim("jobs", "worker-1", lease_seconds=0)
        assert r1 is not None
        assert r1.claimed_by == "worker-1"

        r2 = bus.queue_claim("jobs", "worker-2", lease_seconds=300)
        assert r2 is not None
        assert r2.claimed_by == "worker-2"
        assert r2.message.id == r1.message.id

    def test_active_lease_not_reclaimable(self, bus):
        bus.publish("jobs", "admin", "task", "leased", queue=True)
        bus.queue_claim("jobs", "worker-1", lease_seconds=300)
        assert bus.queue_claim("jobs", "worker-2") is None

    def test_ack_after_lease_expired_still_works(self, bus):
        bus.publish("jobs", "admin", "task", "leased", queue=True)
        r1 = bus.queue_claim("jobs", "worker-1", lease_seconds=0)
        assert r1 is not None
        acked = bus.queue_ack(r1.message.id, "worker-1")
        assert acked is not None
        assert acked.status == "completed"

    def test_reclaimed_message_original_cant_ack(self, bus):
        bus.publish("jobs", "admin", "task", "leased", queue=True)
        r1 = bus.queue_claim("jobs", "worker-1", lease_seconds=0)
        assert r1 is not None
        bus.queue_claim("jobs", "worker-2", lease_seconds=300)
        assert bus.queue_ack(r1.message.id, "worker-1") is None


class TestClientQueueAPI:
    def test_client_queue_publish_claim_ack(self):
        from mansio import MansioClient

        bus = Bus(backend=MemoryBackend())
        admin = MansioClient(bus, "admin-bot")
        worker = MansioClient(bus, "worker-bot")

        admin.queue_publish("task-queue", "build the thing")
        result = worker.queue_claim("task-queue")
        assert result is not None
        assert result.message.payload == "build the thing"
        assert result.claimed_by == "worker-bot"

        acked = worker.queue_ack(result.message.id)
        assert acked is not None
        assert acked.status == "completed"

        assert worker.queue_claim("task-queue") is None

        admin.close()
        worker.close()
        bus.close()
