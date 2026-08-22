"""Tests for system channel compaction.

Verifies that _system:registry and _system:cursors:* channels are
automatically compacted to prevent unbounded growth.

Closes #46.
"""

from __future__ import annotations

import pytest

from piazza import Bus, MemoryBackend
from piazza.backends.sqlite import SQLiteBackend


@pytest.fixture(params=["memory", "sqlite"])
def bus(request, tmp_path):
    if request.param == "memory":
        b = Bus(backend=MemoryBackend())
    else:
        b = Bus(backend=SQLiteBackend(tmp_path / "test.db"))
    yield b
    b.close()


@pytest.fixture(params=["memory", "sqlite"])
def backend_and_bus(request, tmp_path):
    backend = MemoryBackend() if request.param == "memory" else SQLiteBackend(tmp_path / "test.db")
    b = Bus(backend=backend)
    yield backend, b
    b.close()


class TestRegistryCompaction:
    """_system:registry keeps only the latest message per sender."""

    def test_single_agent_multiple_registrations(self, bus):
        """Repeated registrations from same agent collapse to one."""
        for i in range(10):
            bus.publish(
                "_system:registry",
                "agent-a",
                "register",
                f'{{"agent_id": "agent-a", "round": {i}}}',
                metadata={"secret_hash": f"hash-{i}", "action": "register"},
            )

        msgs = bus.poll("_system:registry", limit=1000)
        assert len(msgs) == 1
        assert msgs[0].sender == "agent-a"
        # Should be the latest registration.
        assert '"round": 9' in msgs[0].payload

    def test_multiple_agents_each_kept(self, bus):
        """Each agent keeps exactly one entry after compaction."""
        for agent in ("alice", "bob", "carol"):
            for i in range(5):
                bus.publish(
                    "_system:registry",
                    agent,
                    "register",
                    f'{{"agent_id": "{agent}", "round": {i}}}',
                    metadata={"secret_hash": f"hash-{agent}-{i}"},
                )

        msgs = bus.poll("_system:registry", limit=1000)
        senders = [m.sender for m in msgs]
        assert sorted(senders) == ["alice", "bob", "carol"]
        for m in msgs:
            assert '"round": 4' in m.payload

    def test_non_system_channels_not_compacted(self, bus):
        """Regular channels are unaffected by compaction logic."""
        for i in range(10):
            bus.publish("general", "agent-a", "text", f"msg-{i}")

        msgs = bus.poll("general", limit=1000)
        assert len(msgs) == 10


class TestCursorCompaction:
    """_system:cursors:* keeps only the latest snapshot."""

    def test_cursor_saves_collapse(self, bus):
        """Repeated cursor saves leave only the latest."""
        ch = "_system:cursors:agent-x"
        for i in range(10):
            bus.publish(ch, "agent-x", "cursor_snapshot", f'{{"cursor": {i}}}')

        msgs = bus.poll(ch, limit=1000)
        assert len(msgs) == 1
        assert '"cursor": 9' in msgs[0].payload

    def test_different_cursor_channels_independent(self, bus):
        """Each agent's cursor channel compacts independently."""
        for agent in ("alpha", "beta"):
            ch = f"_system:cursors:{agent}"
            for i in range(5):
                bus.publish(ch, agent, "cursor_snapshot", f'{{"n": {i}}}')

        assert len(bus.poll("_system:cursors:alpha", limit=100)) == 1
        assert len(bus.poll("_system:cursors:beta", limit=100)) == 1


class TestCompactMethodDirect:
    """Direct Backend.compact() API tests."""

    def test_compact_max_messages(self, backend_and_bus):
        backend, bus = backend_and_bus
        for i in range(20):
            bus.publish("test-ch", "agent", "text", f"msg-{i}")

        removed = backend.compact("test-ch", max_messages=5)
        assert removed == 15
        msgs = bus.poll("test-ch", limit=100)
        assert len(msgs) == 5
        # Should keep the latest 5.
        payloads = [m.payload for m in msgs]
        assert payloads == [f"msg-{i}" for i in range(15, 20)]

    def test_compact_keep_latest_per_sender(self, backend_and_bus):
        backend, bus = backend_and_bus
        for agent in ("a", "b", "c"):
            for i in range(5):
                bus.publish("ch", agent, "text", f"{agent}-{i}")

        removed = backend.compact("ch", keep_latest_per_sender=True)
        assert removed == 12  # 15 total - 3 kept
        msgs = bus.poll("ch", limit=100)
        assert len(msgs) == 3
        senders = {m.sender for m in msgs}
        assert senders == {"a", "b", "c"}

    def test_compact_empty_channel(self, backend_and_bus):
        backend, _ = backend_and_bus
        removed = backend.compact("nonexistent", max_messages=5)
        assert removed == 0

    def test_compact_both_options(self, backend_and_bus):
        """keep_latest_per_sender + max_messages combined."""
        backend, bus = backend_and_bus
        for agent in ("x", "y", "z"):
            for i in range(5):
                bus.publish("ch", agent, "text", f"{agent}-{i}")

        # Dedup first → 3 msgs, then max_messages=2 → keep latest 2.
        removed = backend.compact("ch", keep_latest_per_sender=True, max_messages=2)
        assert removed == 13  # 15 total - 2 kept
        msgs = bus.poll("ch", limit=100)
        assert len(msgs) == 2
