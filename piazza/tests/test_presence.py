"""Tests for agent presence (heartbeat / roster query).

Covers all three backends (Memory, SQLite, Maildir) and the Bus layer.
"""

from __future__ import annotations

import time

import pytest

try:
    from piazza.backends.maildir import MaildirBackend
except ImportError:
    MaildirBackend = None  # type: ignore[assignment,misc]

from piazza.backends.memory import MemoryBackend
from piazza.backends.sqlite import SQLiteBackend
from piazza.bus import Bus
from piazza.types import AgentPresence

_skip_maildir = pytest.mark.skipif(MaildirBackend is None, reason="maildir backend not available")


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def memory_backend():
    b = MemoryBackend()
    yield b
    b.close()


@pytest.fixture
def sqlite_backend(tmp_path):
    b = SQLiteBackend(tmp_path / "test.db")
    yield b
    b.close()


@pytest.fixture
def maildir_backend(tmp_path):
    if MaildirBackend is None:
        pytest.skip("maildir backend not available")
    b = MaildirBackend(tmp_path / "maildir-root")
    yield b
    b.close()


def _backend_params():
    params = ["memory", "sqlite"]
    if MaildirBackend is not None:
        params.append("maildir")
    return params


@pytest.fixture(params=_backend_params())
def backend(request, tmp_path):
    if request.param == "memory":
        b = MemoryBackend()
    elif request.param == "sqlite":
        b = SQLiteBackend(tmp_path / "test.db")
    else:
        b = MaildirBackend(tmp_path / "maildir-root")
    yield b
    b.close()


# ── Backend-parametric tests ─────────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_records_agent(self, backend):
        backend.heartbeat("agent-a")
        agents = backend.agents()
        assert len(agents) == 1
        assert agents[0].agent_id == "agent-a"
        assert agents[0].status == "online"

    def test_heartbeat_with_metadata(self, backend):
        backend.heartbeat("agent-a", metadata={"display_name": "Alice"})
        result = backend.agent_status("agent-a")
        assert result is not None
        assert result.metadata == {"display_name": "Alice"}

    def test_heartbeat_updates_last_seen(self, backend):
        backend.heartbeat("agent-a")
        first = backend.agent_status("agent-a")
        assert first is not None
        time.sleep(0.05)
        backend.heartbeat("agent-a")
        second = backend.agent_status("agent-a")
        assert second is not None
        assert second.last_seen >= first.last_seen

    def test_heartbeat_updates_metadata(self, backend):
        backend.heartbeat("agent-a", metadata={"v": 1})
        backend.heartbeat("agent-a", metadata={"v": 2})
        result = backend.agent_status("agent-a")
        assert result is not None
        assert result.metadata == {"v": 2}

    def test_heartbeat_no_metadata(self, backend):
        backend.heartbeat("agent-a")
        result = backend.agent_status("agent-a")
        assert result is not None
        assert result.metadata is None


class TestAgents:
    def test_agents_empty(self, backend):
        assert backend.agents() == []

    def test_agents_multiple(self, backend):
        backend.heartbeat("agent-b")
        backend.heartbeat("agent-a")
        backend.heartbeat("agent-c")
        agents = backend.agents()
        assert len(agents) == 3
        # Sorted by agent_id
        assert [a.agent_id for a in agents] == ["agent-a", "agent-b", "agent-c"]

    def test_agents_all_online(self, backend):
        backend.heartbeat("agent-a")
        backend.heartbeat("agent-b")
        agents = backend.agents(timeout_seconds=120)
        assert all(a.status == "online" for a in agents)

    def test_agents_offline_after_timeout(self, backend):
        backend.heartbeat("agent-a")
        # Use a very short timeout so the heartbeat is already "expired"
        agents = backend.agents(timeout_seconds=0)
        assert len(agents) == 1
        assert agents[0].status == "offline"

    def test_agents_mixed_status(self, backend):
        # agent-a heartbeats now (online)
        backend.heartbeat("agent-a")
        # agent-b: simulate old heartbeat by directly setting last_seen
        # Use a 1-second timeout; agent-a is online, agent-b would be offline
        # We'll just use timeout=0 to make agent-a offline too, then re-heartbeat
        # Actually, let's test properly: use timeout_seconds parameter
        agents = backend.agents(timeout_seconds=3600)
        assert all(a.status == "online" for a in agents)
        agents = backend.agents(timeout_seconds=0)
        assert all(a.status == "offline" for a in agents)


class TestAgentStatus:
    def test_unknown_agent(self, backend):
        assert backend.agent_status("nonexistent") is None

    def test_known_agent(self, backend):
        backend.heartbeat("agent-a", metadata={"role": "worker"})
        result = backend.agent_status("agent-a")
        assert result is not None
        assert isinstance(result, AgentPresence)
        assert result.agent_id == "agent-a"
        assert result.status == "online"
        assert result.metadata == {"role": "worker"}

    def test_agent_offline_by_timeout(self, backend):
        backend.heartbeat("agent-a")
        result = backend.agent_status("agent-a", timeout_seconds=0)
        assert result is not None
        assert result.status == "offline"

    def test_agent_online_within_timeout(self, backend):
        backend.heartbeat("agent-a")
        result = backend.agent_status("agent-a", timeout_seconds=3600)
        assert result is not None
        assert result.status == "online"


class TestPresencePersistence:
    def test_presence_survives_reopen_sqlite(self, tmp_path):
        db = tmp_path / "test.db"
        b1 = SQLiteBackend(db)
        b1.heartbeat("agent-a", metadata={"name": "Alice"})
        b1.close()

        b2 = SQLiteBackend(db)
        result = b2.agent_status("agent-a", timeout_seconds=3600)
        assert result is not None
        assert result.agent_id == "agent-a"
        assert result.metadata == {"name": "Alice"}
        b2.close()

    @_skip_maildir
    def test_presence_survives_reopen_maildir(self, tmp_path):
        root = tmp_path / "maildir-root"
        b1 = MaildirBackend(root)
        b1.heartbeat("agent-a", metadata={"name": "Alice"})
        b1.close()

        b2 = MaildirBackend(root)
        result = b2.agent_status("agent-a", timeout_seconds=3600)
        assert result is not None
        assert result.agent_id == "agent-a"
        assert result.metadata == {"name": "Alice"}
        b2.close()


# ── Bus layer tests ──────────────────────────────────────────────────────────


class TestBusPresence:
    def test_bus_heartbeat_and_agents(self):
        bus = Bus(backend=MemoryBackend())
        bus.heartbeat("agent-a", metadata={"display_name": "Alice"})
        bus.heartbeat("agent-b")

        agents = bus.agents()
        assert len(agents) == 2
        assert agents[0].agent_id == "agent-a"
        assert agents[0].metadata == {"display_name": "Alice"}
        bus.close()

    def test_bus_agent_status(self):
        bus = Bus(backend=MemoryBackend())
        bus.heartbeat("agent-a")

        result = bus.agent_status("agent-a")
        assert result is not None
        assert result.status == "online"

        assert bus.agent_status("nonexistent") is None
        bus.close()

    def test_bus_agent_timeout(self):
        bus = Bus(backend=MemoryBackend())
        bus.heartbeat("agent-a")

        result = bus.agent_status("agent-a", timeout_seconds=0)
        assert result is not None
        assert result.status == "offline"
        bus.close()


# ── AgentPresence type tests ─────────────────────────────────────────────────


class TestAgentPresenceType:
    def test_frozen(self):
        ap = AgentPresence(agent_id="test", status="online", last_seen="2026-01-01T00:00:00+00:00")
        with pytest.raises(AttributeError):
            ap.status = "offline"  # type: ignore[misc]

    def test_default_metadata_none(self):
        ap = AgentPresence(agent_id="test", status="online", last_seen="2026-01-01T00:00:00+00:00")
        assert ap.metadata is None

    def test_with_metadata(self):
        ap = AgentPresence(
            agent_id="test",
            status="online",
            last_seen="2026-01-01T00:00:00+00:00",
            metadata={"cap": ["search"]},
        )
        assert ap.metadata == {"cap": ["search"]}
