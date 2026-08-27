"""Tests for MansioClient presence, queue_status, and subscribe/unsubscribe."""

from __future__ import annotations

import time

from .conftest import make_client

# ──────────────────────────────────────────────────────────────────
# Presence: heartbeat / agents / agent_status
# ──────────────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_sends_presence(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-alpha")
        client.heartbeat()
        result = bus.agent_status("agent-alpha")
        assert result is not None
        assert result.agent_id == "agent-alpha"
        assert result.status == "online"
        client.close()

    def test_heartbeat_with_metadata(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-alpha")
        client.heartbeat(metadata={"display_name": "Alpha", "version": "1.0"})
        result = bus.agent_status("agent-alpha")
        assert result is not None
        assert result.metadata == {"display_name": "Alpha", "version": "1.0"}
        client.close()


class TestAgents:
    def test_agents_returns_online_agents(self, mansio_server):
        url, store, bus, server = mansio_server
        client_a = make_client(url, store, "agent-aaa")
        client_b = make_client(url, store, "agent-bbb")
        client_a.heartbeat()
        client_b.heartbeat()
        agents = client_a.agents()
        assert len(agents) == 2
        ids = [a.agent_id for a in agents]
        assert "agent-aaa" in ids
        assert "agent-bbb" in ids
        assert all(a.status == "online" for a in agents)
        client_a.close()
        client_b.close()


class TestAgentStatus:
    def test_agent_status_returns_single(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-gamma")
        client.heartbeat(metadata={"role": "worker"})
        result = client.agent_status("agent-gamma")
        assert result is not None
        assert result.agent_id == "agent-gamma"
        assert result.status == "online"
        assert result.metadata == {"role": "worker"}
        client.close()

    def test_agent_status_unknown_returns_none(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-gamma")
        result = client.agent_status("nonexistent")
        assert result is None
        client.close()

    def test_agent_status_offline_after_timeout(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-gamma")
        client.heartbeat()
        time.sleep(1.5)
        result = client.agent_status("agent-gamma", timeout_seconds=1)
        assert result is not None
        assert result.status == "offline"
        client.close()


# ──────────────────────────────────────────────────────────────────
# Queue status
# ──────────────────────────────────────────────────────────────────


class TestQueueStatus:
    def test_queue_status_returns_claim_state(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-delta")
        msg_id = client.queue_publish("tasks", "do something")
        client.queue_claim("tasks")
        status = client.queue_status(msg_id)
        assert status is not None
        assert status["status"] == "claimed"
        assert status["claimed_by"] == "agent-delta"
        client.close()

    def test_queue_status_unclaimed(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-delta")
        msg_id = client.queue_publish("tasks", "pending task")
        status = client.queue_status(msg_id)
        assert status is not None
        assert status["status"] == "unclaimed"
        client.close()

    def test_queue_status_nonexistent_returns_none(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-delta")
        status = client.queue_status("nonexistent-id")
        assert status is None
        client.close()

    def test_queue_status_completed(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-delta")
        msg_id = client.queue_publish("tasks", "finish me")
        client.queue_claim("tasks")
        client.queue_ack(msg_id)
        status = client.queue_status(msg_id)
        assert status is not None
        assert status["status"] == "completed"
        client.close()
