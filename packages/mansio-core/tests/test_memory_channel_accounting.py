"""MemoryBackend channel accounting must match its visible channel list.

``queue_retire()`` used to leave an empty list under a channel whose last
message it removed. ``stats()`` and ``info()`` then counted a channel that
``list_channels()`` no longer reported, and that SQLite's
``COUNT(DISTINCT channel)`` never counts.
"""

from __future__ import annotations

import pytest
from mansio_core import Bus, MemoryBackend
from mansio_core.backends import SQLiteBackend


def _drain_queue(bus: Bus, channel: str, count: int) -> None:
    """Claim and acknowledge *count* queue messages."""
    for _ in range(count):
        claim = bus.queue_claim(channel, "worker")
        assert claim is not None
        assert bus.queue_ack(claim.message.id, "worker") is not None


@pytest.mark.parametrize(
    "make_backend",
    [MemoryBackend, lambda: SQLiteBackend(":memory:")],
    ids=["memory", "sqlite"],
)
def test_stats_ignore_channel_emptied_by_queue_retire(make_backend):
    bus = Bus(backend=make_backend())
    for i in range(3):
        bus.publish("jobs", "producer", "task", f"task-{i}", queue=True)
    _drain_queue(bus, "jobs", 3)

    assert bus.backend.queue_retire(max_age_seconds=0) == 3

    assert bus.channels() == []
    assert bus.stats()["total_channels"] == 0
    assert bus.info()["total_channels"] == 0


def test_stats_count_remaining_channels_alongside_emptied_one():
    bus = Bus(backend=MemoryBackend())
    bus.publish("keep", "agent", "text", "hello")
    for i in range(2):
        bus.publish("jobs", "producer", "task", f"task-{i}", queue=True)
    _drain_queue(bus, "jobs", 2)

    assert bus.backend.queue_retire(max_age_seconds=0) == 2

    assert bus.channels() == ["keep"]
    assert bus.stats()["total_channels"] == 1
    assert bus.info()["total_channels"] == 1


def test_retired_channel_can_be_reused():
    bus = Bus(backend=MemoryBackend())
    bus.publish("jobs", "producer", "task", "task-0", queue=True)
    _drain_queue(bus, "jobs", 1)
    bus.backend.queue_retire(max_age_seconds=0)

    bus.publish("jobs", "producer", "task", "task-1", queue=True)

    assert bus.channels() == ["jobs"]
    assert bus.stats()["total_channels"] == 1
    assert bus.backend.message_count("jobs") == 1


class TestCompactQueueStatus:
    """compact() must forget queue status for every message it removes."""

    @pytest.mark.parametrize(
        "make_backend",
        [MemoryBackend, lambda: SQLiteBackend(":memory:")],
        ids=["memory", "sqlite"],
    )
    def test_trim_drops_status_of_removed_queue_messages(self, make_backend):
        bus = Bus(backend=make_backend())
        ids = [bus.publish("jobs", "producer", "task", f"task-{i}", queue=True) for i in range(3)]
        _drain_queue(bus, "jobs", 3)

        assert bus.backend.compact("jobs", max_messages=1) == 2

        assert bus.backend.queue_status(ids[0]) is None
        assert bus.backend.queue_status(ids[1]) is None
        assert bus.backend.queue_status(ids[2]) is not None
        assert bus.backend.queue_stats() == {"unclaimed": 0, "claimed": 0, "completed": 1}

    @pytest.mark.parametrize(
        "make_backend",
        [MemoryBackend, lambda: SQLiteBackend(":memory:")],
        ids=["memory", "sqlite"],
    )
    def test_dedup_drops_status_of_removed_queue_messages(self, make_backend):
        bus = Bus(backend=make_backend())
        first_a = bus.publish("jobs", "a", "task", "a1", queue=True)
        first_b = bus.publish("jobs", "b", "task", "b1", queue=True)
        last_a = bus.publish("jobs", "a", "task", "a2", queue=True)
        _drain_queue(bus, "jobs", 3)

        assert bus.backend.compact("jobs", keep_latest_per_sender=True) == 1

        assert bus.backend.queue_status(first_a) is None
        assert bus.backend.queue_status(first_b) is not None
        assert bus.backend.queue_status(last_a) is not None
        assert bus.backend.queue_stats()["completed"] == 2
