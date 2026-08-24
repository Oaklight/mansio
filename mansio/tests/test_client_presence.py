"""Tests for MansioClient presence, queue_status, and subscribe/unsubscribe."""

from __future__ import annotations

from mansio import Bus, MansioClient, MemoryBackend

# ──────────────────────────────────────────────────────────────────
# Presence: heartbeat / agents / agent_status
# ──────────────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_sends_presence(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-alpha") as client:
            client.heartbeat()
            result = bus.agent_status("agent-alpha")
            assert result is not None
            assert result.agent_id == "agent-alpha"
            assert result.status == "online"

    def test_heartbeat_with_metadata(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-alpha") as client:
            client.heartbeat(metadata={"display_name": "Alpha", "version": "1.0"})
            result = bus.agent_status("agent-alpha")
            assert result is not None
            assert result.metadata == {"display_name": "Alpha", "version": "1.0"}


class TestAgents:
    def test_agents_returns_online_agents(self):
        with (
            Bus(backend=MemoryBackend()) as bus,
            MansioClient(bus, "agent-aaa") as client_a,
            MansioClient(bus, "agent-bbb") as client_b,
        ):
            client_a.heartbeat()
            client_b.heartbeat()
            agents = client_a.agents()
            assert len(agents) == 2
            ids = [a.agent_id for a in agents]
            assert "agent-aaa" in ids
            assert "agent-bbb" in ids
            assert all(a.status == "online" for a in agents)


class TestAgentStatus:
    def test_agent_status_returns_single(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-gamma") as client:
            client.heartbeat(metadata={"role": "worker"})
            result = client.agent_status("agent-gamma")
            assert result is not None
            assert result.agent_id == "agent-gamma"
            assert result.status == "online"
            assert result.metadata == {"role": "worker"}

    def test_agent_status_unknown_returns_none(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-gamma") as client:
            result = client.agent_status("nonexistent")
            assert result is None

    def test_agent_status_offline_after_timeout(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-gamma") as client:
            client.heartbeat()
            result = client.agent_status("agent-gamma", timeout_seconds=0)
            assert result is not None
            assert result.status == "offline"


# ──────────────────────────────────────────────────────────────────
# Queue status
# ──────────────────────────────────────────────────────────────────


class TestQueueStatus:
    def test_queue_status_returns_claim_state(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-delta") as client:
            msg_id = client.queue_publish("tasks", "do something")
            client.queue_claim("tasks")
            status = client.queue_status(msg_id)
            assert status is not None
            assert status["status"] == "claimed"
            assert status["claimed_by"] == "agent-delta"

    def test_queue_status_unclaimed(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-delta") as client:
            msg_id = client.queue_publish("tasks", "pending task")
            status = client.queue_status(msg_id)
            assert status is not None
            assert status["status"] == "unclaimed"

    def test_queue_status_nonexistent_returns_none(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-delta") as client:
            status = client.queue_status("nonexistent-id")
            assert status is None

    def test_queue_status_completed(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-delta") as client:
            msg_id = client.queue_publish("tasks", "finish me")
            client.queue_claim("tasks")
            client.queue_ack(msg_id)
            status = client.queue_status(msg_id)
            assert status is not None
            assert status["status"] == "completed"


# ──────────────────────────────────────────────────────────────────
# Subscribe / Unsubscribe
# ──────────────────────────────────────────────────────────────────


class TestSubscribe:
    def test_subscribe_receives_messages(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-echo") as client:
            received: list = []
            sub_id = client.subscribe("general", lambda msg: received.append(msg))
            client.channel_send("general", "hello from echo")
            assert len(received) == 1
            assert received[0].payload == "hello from echo"
            assert received[0].channel == "general"
            client.unsubscribe(sub_id)

    def test_unsubscribe_stops_messages(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-foxtrot") as client:
            received: list = []
            sub_id = client.subscribe("events", lambda msg: received.append(msg))
            client.channel_send("events", "first")
            assert len(received) == 1
            client.unsubscribe(sub_id)
            client.channel_send("events", "second")
            assert len(received) == 1

    def test_subscribe_multiple_channels(self):
        with Bus(backend=MemoryBackend()) as bus, MansioClient(bus, "agent-golf") as client:
            received_a: list = []
            received_b: list = []
            sub_a = client.subscribe("chan-a", lambda msg: received_a.append(msg))
            sub_b = client.subscribe("chan-b", lambda msg: received_b.append(msg))
            client.channel_send("chan-a", "msg-a")
            client.channel_send("chan-b", "msg-b")
            assert len(received_a) == 1
            assert received_a[0].payload == "msg-a"
            assert len(received_b) == 1
            assert received_b[0].payload == "msg-b"
            client.unsubscribe(sub_a)
            client.unsubscribe(sub_b)
