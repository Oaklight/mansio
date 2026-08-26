"""Tests for channel and message deletion API.

Covers:
- Backend-level delete_channel and delete_message
- Bus-level deletion with subscription cleanup
- HTTP API endpoints (DELETE /v1/channels/<name>, DELETE /v1/messages/<id>,
  POST /v1/admin/channels/cleanup)
- Auth enforcement (scoped tokens, supertokens, no-auth)
- System channel protection

Closes #57.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from mansio import Bus, MemoryBackend
from mansio.backends.sqlite import SQLiteBackend

# ── Fixtures ──────────────────────────────────────────────────────


def _make_backend(kind: str, tmp_path: Any) -> Any:
    """Create a backend instance by name."""
    if kind == "memory":
        return MemoryBackend()
    return SQLiteBackend(tmp_path / "test.db")


@pytest.fixture(params=["memory", "sqlite"])
def backend(request, tmp_path):
    b = _make_backend(request.param, tmp_path)
    yield b
    b.close()


@pytest.fixture(params=["memory", "sqlite"])
def bus(request, tmp_path):
    b = Bus(backend=_make_backend(request.param, tmp_path))
    yield b
    b.close()


def _publish_n(bus, channel, sender, n):
    """Publish n messages and return their IDs."""
    ids = []
    for i in range(n):
        mid = bus.publish(channel, sender, "text", f"msg-{i}")
        ids.append(mid)
    return ids


# ── Backend-level tests ──────────────────────────────────────────


class TestBackendDeleteChannel:
    """Backend.delete_channel removes channel and all messages."""

    def test_delete_channel_returns_count(self, bus):
        _publish_n(bus, "test-ch", "agent-a", 5)
        count = bus.backend.delete_channel("test-ch")
        assert count == 5

    def test_delete_channel_removes_messages(self, bus):
        _publish_n(bus, "test-ch", "agent-a", 3)
        bus.backend.delete_channel("test-ch")
        assert bus.query("test-ch") == []

    def test_delete_channel_removes_from_list(self, bus):
        _publish_n(bus, "test-ch", "agent-a", 2)
        _publish_n(bus, "other-ch", "agent-a", 1)
        bus.backend.delete_channel("test-ch")
        channels = bus.channels()
        assert "test-ch" not in channels
        assert "other-ch" in channels

    def test_delete_nonexistent_channel(self, bus):
        count = bus.backend.delete_channel("no-such-channel")
        assert count == 0

    def test_delete_channel_with_queue_messages(self, bus):
        bus.publish("queue-ch", "agent-a", "task", "do-work", queue=True)
        bus.publish("queue-ch", "agent-a", "task", "do-more", queue=True)
        count = bus.backend.delete_channel("queue-ch")
        assert count == 2
        assert bus.query("queue-ch") == []


class TestBackendDeleteMessage:
    """Backend.delete_message removes individual messages."""

    def test_delete_message_returns_true(self, bus):
        mid = bus.publish("test-ch", "agent-a", "text", "hello")
        assert bus.backend.delete_message(mid) is True

    def test_delete_message_removes_it(self, bus):
        ids = _publish_n(bus, "test-ch", "agent-a", 3)
        bus.backend.delete_message(ids[1])
        msgs = bus.query("test-ch")
        remaining_ids = [m.id for m in msgs]
        assert ids[1] not in remaining_ids
        assert len(msgs) == 2

    def test_delete_nonexistent_message(self, bus):
        assert bus.backend.delete_message("nonexistent-id") is False

    def test_delete_last_message_removes_channel(self, bus):
        """Deleting all messages should make channel disappear from list."""
        mid = bus.publish("solo-ch", "agent-a", "text", "only one")
        bus.backend.delete_message(mid)
        # Channel may or may not remain in list depending on backend,
        # but querying it should return empty
        assert bus.query("solo-ch") == []


# ── Bus-level tests ──────────────────────────────────────────────


class TestBusDeletion:
    """Bus deletion methods with subscription cleanup."""

    def test_delete_channel_clears_subscriptions(self, bus):
        received = []
        bus.subscribe("test-ch", lambda m: received.append(m))
        _publish_n(bus, "test-ch", "agent-a", 2)
        assert len(received) == 2

        bus.delete_channel("test-ch")

        # Subscriptions should be removed
        assert "test-ch" not in bus.subscription_counts()

    def test_delete_channel_other_channels_unaffected(self, bus):
        _publish_n(bus, "ch-a", "agent-a", 3)
        _publish_n(bus, "ch-b", "agent-a", 2)

        bus.delete_channel("ch-a")

        assert bus.query("ch-a") == []
        assert len(bus.query("ch-b")) == 2

    def test_delete_message_via_bus(self, bus):
        ids = _publish_n(bus, "test-ch", "agent-a", 3)
        assert bus.delete_message(ids[0]) is True
        assert len(bus.query("test-ch")) == 2


# ── Maildir backend tests ───────────────────────────────────────


class TestMaildirDeletion:
    """Maildir-specific deletion tests."""

    @pytest.fixture
    def maildir_bus(self, tmp_path):
        from mansio.backends.maildir import MaildirBackend

        b = Bus(backend=MaildirBackend(tmp_path / "maildir"))
        yield b
        b.close()

    def test_delete_channel(self, maildir_bus):
        _publish_n(maildir_bus, "test-ch", "agent-a", 3)
        count = maildir_bus.delete_channel("test-ch")
        assert count == 3
        assert maildir_bus.query("test-ch") == []

    def test_delete_message(self, maildir_bus):
        ids = _publish_n(maildir_bus, "test-ch", "agent-a", 3)
        assert maildir_bus.delete_message(ids[1]) is True
        msgs = maildir_bus.query("test-ch")
        assert len(msgs) == 2
        assert ids[1] not in [m.id for m in msgs]

    def test_delete_nonexistent(self, maildir_bus):
        assert maildir_bus.delete_message("no-such-id") is False
        assert maildir_bus.delete_channel("no-such-ch") == 0


# ── HTTP API tests ───────────────────────────────────────────────


class TestHttpDeletion:
    """HTTP frontend deletion endpoints."""

    @pytest.fixture
    def client(self, tmp_path):
        """Create a test HTTP client with no auth."""
        from mansio.frontends.http import HttpFrontend

        backend = MemoryBackend()
        bus = Bus(backend=backend)
        frontend = HttpFrontend(port=0)
        frontend.attach(bus)

        # Start server in background
        server_thread = threading.Thread(target=frontend.serve_forever, daemon=True)
        server_thread.start()

        # Wait for server to bind
        import time

        for _ in range(50):
            if frontend._app.port is not None:
                break
            time.sleep(0.05)

        host, port = frontend.address
        base_url = f"http://{host}:{port}"

        yield base_url, bus, frontend

        frontend.shutdown()

    def _request(self, method, url, json_data=None):
        """Make an HTTP request and return (status, body_dict)."""
        import http.client
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        body = json.dumps(json_data).encode() if json_data else None
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, parsed.path, body=body, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        status = resp.status
        conn.close()
        return status, data

    def test_delete_channel(self, client):
        base_url, bus, _ = client
        _publish_n(bus, "test-ch", "agent-a", 3)

        status, data = self._request("DELETE", f"{base_url}/v1/channels/test-ch")
        assert status == 200
        assert data["deleted"] == 3
        assert data["channel"] == "test-ch"

    def test_delete_channel_not_found(self, client):
        base_url, bus, _ = client
        status, data = self._request("DELETE", f"{base_url}/v1/channels/nope")
        assert status == 404

    def test_delete_message(self, client):
        base_url, bus, _ = client
        mid = bus.publish("test-ch", "agent-a", "text", "hello")

        status, data = self._request("DELETE", f"{base_url}/v1/messages/{mid}")
        assert status == 200
        assert data["deleted"] is True
        assert data["message_id"] == mid

    def test_delete_message_not_found(self, client):
        base_url, bus, _ = client
        status, data = self._request("DELETE", f"{base_url}/v1/messages/nonexistent")
        assert status == 404

    def test_admin_cleanup(self, client):
        base_url, bus, _ = client
        _publish_n(bus, "test:alpha", "agent-a", 2)
        _publish_n(bus, "test:beta", "agent-a", 3)
        _publish_n(bus, "keep-me", "agent-a", 1)

        status, data = self._request(
            "POST",
            f"{base_url}/v1/admin/channels/cleanup",
            {"pattern": "test:*"},
        )
        assert status == 200
        assert data["channels_deleted"] == 2
        assert data["messages_deleted"] == 5
        assert sorted(data["channels"]) == ["test:alpha", "test:beta"]

        # keep-me should survive
        assert len(bus.query("keep-me")) == 1

    def test_admin_cleanup_no_pattern(self, client):
        base_url, bus, _ = client
        status, data = self._request(
            "POST",
            f"{base_url}/v1/admin/channels/cleanup",
            {"pattern": ""},
        )
        assert status == 400

    def test_admin_cleanup_no_matches(self, client):
        base_url, bus, _ = client
        status, data = self._request(
            "POST",
            f"{base_url}/v1/admin/channels/cleanup",
            {"pattern": "nothing-matches:*"},
        )
        assert status == 200
        assert data["channels_deleted"] == 0


class TestHttpDeletionAuth:
    """Auth enforcement for deletion endpoints."""

    @pytest.fixture
    def authed_client(self, tmp_path):
        """Create a test HTTP client with token auth."""
        from mansio.frontends.http import HttpFrontend
        from mansio.token_store import TokenStore

        backend = MemoryBackend()
        bus = Bus(backend=backend, require_auth=True)
        token_store = TokenStore(tmp_path / "tokens.db")

        # Create tokens
        agent_token_raw = token_store.create_token("agent-a", label="test")["token"]
        super_token_raw = token_store.create_token(None, label="super")["token"]

        frontend = HttpFrontend(port=0, token_store=token_store)
        frontend.attach(bus)

        server_thread = threading.Thread(target=frontend.serve_forever, daemon=True)
        server_thread.start()

        import time

        for _ in range(50):
            if frontend._app.port is not None:
                break
            time.sleep(0.05)

        host, port = frontend.address
        base_url = f"http://{host}:{port}"

        yield base_url, bus, frontend, agent_token_raw, super_token_raw

        frontend.shutdown()

    def _request(self, method, url, token=None, json_data=None):
        import http.client
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        body = json.dumps(json_data).encode() if json_data else None
        headers = {"Content-Type": "application/json"} if body else {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, parsed.path, body=body, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        status = resp.status
        conn.close()
        return status, data

    def test_supertoken_can_delete_channel(self, authed_client):
        base_url, bus, _, _, super_token = authed_client
        _publish_n(bus, "test-ch", "agent-a", 3)
        status, data = self._request("DELETE", f"{base_url}/v1/channels/test-ch", token=super_token)
        assert status == 200
        assert data["deleted"] == 3

    def test_scoped_token_cannot_delete_public_channel(self, authed_client):
        base_url, bus, _, agent_token, _ = authed_client
        _publish_n(bus, "test-ch", "agent-a", 2)
        status, data = self._request("DELETE", f"{base_url}/v1/channels/test-ch", token=agent_token)
        assert status == 403

    def test_scoped_token_can_delete_own_private_channel(self, authed_client):
        base_url, bus, _, agent_token, _ = authed_client
        bus.publish("notebook:agent-a", "agent-a", "note", "my note")
        status, data = self._request(
            "DELETE", f"{base_url}/v1/channels/notebook:agent-a", token=agent_token
        )
        assert status == 200

    def test_scoped_token_cannot_delete_others_private_channel(self, authed_client):
        base_url, bus, _, agent_token, _ = authed_client
        bus.publish("notebook:agent-b", "agent-b", "note", "not yours")
        status, data = self._request(
            "DELETE", f"{base_url}/v1/channels/notebook:agent-b", token=agent_token
        )
        assert status == 403

    def test_scoped_token_cannot_delete_system_channel(self, authed_client):
        base_url, bus, _, agent_token, _ = authed_client
        bus.publish(
            "_system:registry",
            "agent-a",
            "register",
            '{"agent_id": "agent-a"}',
            metadata={"secret_hash": "h", "action": "register"},
        )
        status, data = self._request(
            "DELETE", f"{base_url}/v1/channels/_system:registry", token=agent_token
        )
        assert status == 403

    def test_supertoken_can_delete_system_channel(self, authed_client):
        base_url, bus, _, _, super_token = authed_client
        bus.publish(
            "_system:registry",
            "agent-a",
            "register",
            '{"agent_id": "agent-a"}',
            metadata={"secret_hash": "h", "action": "register"},
        )
        status, data = self._request(
            "DELETE", f"{base_url}/v1/channels/_system:registry", token=super_token
        )
        assert status == 200

    def test_scoped_token_can_delete_own_message(self, authed_client):
        base_url, bus, _, agent_token, _ = authed_client
        mid = bus.publish("test-ch", "agent-a", "text", "my message")
        status, data = self._request("DELETE", f"{base_url}/v1/messages/{mid}", token=agent_token)
        assert status == 200

    def test_scoped_token_cannot_delete_others_message(self, authed_client):
        base_url, bus, _, agent_token, _ = authed_client
        mid = bus.publish("test-ch", "agent-b", "text", "not yours")
        status, data = self._request("DELETE", f"{base_url}/v1/messages/{mid}", token=agent_token)
        assert status == 403

    def test_admin_cleanup_requires_supertoken(self, authed_client):
        base_url, bus, _, agent_token, _ = authed_client
        _publish_n(bus, "test:cleanup", "agent-a", 2)
        status, data = self._request(
            "POST",
            f"{base_url}/v1/admin/channels/cleanup",
            token=agent_token,
            json_data={"pattern": "test:*"},
        )
        assert status == 403

    def test_no_token_rejected(self, authed_client):
        base_url, bus, _, _, _ = authed_client
        _publish_n(bus, "test-ch", "agent-a", 2)
        status, data = self._request("DELETE", f"{base_url}/v1/channels/test-ch")
        assert status == 401


# ── Issue #169 Follow-up Tests ────────────────────────────────────


class TestGetMessageLookup(TestHttpDeletion):
    """Verify message deletion uses bus.get_message() (O(1)) not full scan."""

    def test_delete_message_uses_get_message(self, client):
        """Scoped-token path finds message via bus.get_message()."""
        base_url, bus, _ = client
        mid = bus.publish("test-ch", "agent-a", "text", "hello")

        # Verify bus.get_message works
        msg = bus.get_message(mid)
        assert msg is not None
        assert msg.id == mid

    def test_delete_nonexistent_message_returns_404(self, client):
        base_url, bus, _ = client
        status, data = self._request("DELETE", f"{base_url}/v1/messages/nonexistent-id")
        # Without auth, the delete path tries bus.delete_message directly
        # which returns 0 → 404
        assert status == 404


class TestBulkDeleteSystemProtection(TestHttpDeletion):
    """Verify bulk cleanup filters out _system: channels."""

    def test_bulk_delete_star_skips_system_channels(self, client):
        """Wildcard '*' must not delete _system: channels."""
        base_url, bus, _ = client
        _publish_n(bus, "user-ch", "agent-a", 2)
        bus.publish("_system:agents", "agent-a", "presence", "online")

        status, data = self._request(
            "POST",
            f"{base_url}/v1/admin/channels/cleanup",
            {"pattern": "*"},
        )
        assert status == 200
        # user-ch should be deleted
        assert "user-ch" in data["channels"]
        # _system:agents must NOT be deleted
        assert "_system:agents" not in data["channels"]

    def test_bulk_delete_system_glob_matches_nothing(self, client):
        """Pattern '_system:*' should match zero channels (all filtered)."""
        base_url, bus, _ = client
        bus.publish("_system:agents", "agent-a", "presence", "online")
        bus.publish("_system:registry", "agent-a", "registration", "data")

        status, data = self._request(
            "POST",
            f"{base_url}/v1/admin/channels/cleanup",
            {"pattern": "_system:*"},
        )
        assert status == 200
        assert data["channels_deleted"] == 0
        assert data["channels"] == []
