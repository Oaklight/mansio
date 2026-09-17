"""Tests for MansioClient presence, queue_status, and subscribe/unsubscribe."""

from __future__ import annotations

import time

from conftest import make_client

# ──────────────────────────────────────────────────────────────────
# Presence: heartbeat / users / user_status
# ──────────────────────────────────────────────────────────────────


def _wait_for_presence(bus, user_id: str, deadline: float = 3.0):
    """Return presence for *user_id*, waiting for the background write."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        result = bus.user_status(user_id)
        if result is not None:
            return result
        time.sleep(0.02)
    return None


class TestPresenceFromActivity:
    """Authenticated activity is what makes a user present — no heartbeat."""

    def test_connecting_marks_user_online(self, mansio_server):
        url, store, bus, _srv = mansio_server
        client = make_client(url, store, "agent-active")
        result = _wait_for_presence(bus, "agent-active")
        assert result is not None
        assert result.status == "online"
        client.close()

    def test_users_lists_connected_client_without_heartbeat(self, mansio_server):
        url, store, bus, _srv = mansio_server
        client = make_client(url, store, "agent-roster")
        _wait_for_presence(bus, "agent-roster")
        assert "agent-roster" in [u.user_id for u in client.users()]
        client.close()

    def test_requests_inside_the_throttle_window_do_not_rewrite(self, mansio_server):
        url, store, bus, _srv = mansio_server
        client = make_client(url, store, "agent-throttle")
        first = _wait_for_presence(bus, "agent-throttle")
        assert first is not None
        for _ in range(5):
            client.channel_list()
        assert bus.user_status("agent-throttle").last_seen == first.last_seen
        client.close()

    def test_a_request_past_the_window_refreshes_last_seen(self, mansio_server, monkeypatch):
        from mansio.frontends import http as http_frontend

        url, store, bus, _srv = mansio_server
        client = make_client(url, store, "agent-refresh")
        first = _wait_for_presence(bus, "agent-refresh")
        assert first is not None

        monkeypatch.setattr(http_frontend, "_PRESENCE_REFRESH_SECONDS", 0.0)
        client.channel_list()
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            if bus.user_status("agent-refresh").last_seen > first.last_seen:
                break
            time.sleep(0.02)
        assert bus.user_status("agent-refresh").last_seen > first.last_seen
        client.close()

    def test_issued_token_alone_does_not_make_a_user_present(self, mansio_server):
        _url, store, bus, _srv = mansio_server
        store.create_token(user_id="agent-idle", label="never used")
        assert bus.user_status("agent-idle") is None

    def test_subscription_records_the_subscriber(self, mansio_server):
        url, store, bus, _srv = mansio_server
        client = make_client(url, store, "agent-sub")
        sub_id = client.subscribe("general", lambda _m: None)
        result = _wait_for_presence(bus, "agent-sub")
        assert result is not None
        assert result.status == "online"
        client.unsubscribe(sub_id)
        client.close()


class TestHeartbeat:
    def test_heartbeat_sends_presence(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-alpha")
        client.heartbeat()
        result = bus.user_status("agent-alpha")
        assert result is not None
        assert result.user_id == "agent-alpha"
        assert result.status == "online"
        client.close()

    def test_heartbeat_with_metadata(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-alpha")
        client.heartbeat(metadata={"display_name": "Alpha", "version": "1.0"})
        result = bus.user_status("agent-alpha")
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
        agents = client_a.users()
        assert len(agents) == 2
        ids = [a.user_id for a in agents]
        assert "agent-aaa" in ids
        assert "agent-bbb" in ids
        assert all(a.status == "online" for a in agents)
        client_a.close()
        client_b.close()


class TestAgentStatus:
    def test_user_status_returns_single(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-gamma")
        client.heartbeat(metadata={"role": "worker"})
        result = client.user_status("agent-gamma")
        assert result is not None
        assert result.user_id == "agent-gamma"
        assert result.status == "online"
        assert result.metadata == {"role": "worker"}
        client.close()

    def test_user_status_unknown_returns_none(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-gamma")
        result = client.user_status("nonexistent")
        assert result is None
        client.close()

    def test_user_status_offline_after_timeout(self, mansio_server):
        url, store, bus, server = mansio_server
        client = make_client(url, store, "agent-gamma")
        client.heartbeat()
        time.sleep(1.5)
        result = client.user_status("agent-gamma", timeout_seconds=1)
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
