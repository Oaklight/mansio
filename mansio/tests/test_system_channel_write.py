"""Tests for _system:users write restriction — fixes #101.

Verifies that regular scoped scoped tokens get 403 when POSTing
arbitrary message types to ``_system:users``, while the SDK's
presence announcement (msg_type="presence") and supertokens are
still allowed.
"""

from __future__ import annotations

import threading
import time

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend


@pytest.fixture()
def auth_server(tmp_path):
    """Start a MansioServer with token auth enabled, yield (url, token_store)."""
    from mansio.token_store import TokenStore

    db_path = str(tmp_path / "system_ch_test.db")
    token_store = TokenStore(db_path)

    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0, token_store=token_store)
    server = MansioServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    url = f"http://{host}:{port}"

    yield url, token_store

    server.shutdown()


class TestSystemAgentsChannelWrite:
    """Regular scoped tokens must be denied arbitrary writes to _system:users (#101)."""

    def test_regular_agent_cannot_publish_arbitrary_to_system_agents(self, auth_server) -> None:
        """A scoped scoped token should get 403 when publishing non-presence to _system:users."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store = auth_server
        token_entry = store.create_token("milo", "Milo's token")
        token = token_entry["token"]

        http = HttpClient(headers={"Authorization": f"Bearer {token}"})
        resp = http.post(
            f"{url}/v1/publish",
            json={
                "channel": "_system:users",
                "sender": "milo",
                "msg_type": "chat",
                "payload": "should be rejected",
            },
        )
        assert resp.status_code == 403, (
            f"Expected 403 for regular agent writing chat to _system:users, got {resp.status_code}"
        )
        http.close()

    def test_regular_agent_can_announce_presence(self, auth_server) -> None:
        """A scoped scoped token can publish msg_type=presence to _system:users (SDK _announce)."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store = auth_server
        token_entry = store.create_token("milo", "Milo's token")
        token = token_entry["token"]

        http = HttpClient(headers={"Authorization": f"Bearer {token}"})
        resp = http.post(
            f"{url}/v1/publish",
            json={
                "channel": "_system:users",
                "sender": "milo",
                "msg_type": "presence",
                "payload": '{"status": "online"}',
            },
        )
        assert resp.status_code == 200, (
            f"Expected 200 for presence announce to _system:users, got {resp.status_code}"
        )
        http.close()

    def test_supertoken_can_publish_to_system_agents(self, auth_server) -> None:
        """A supertoken (user_id=NULL) should be able to write anything to _system:users."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store = auth_server
        token_entry = store.create_token(user_id=None, label="Admin supertoken")
        token = token_entry["token"]

        http = HttpClient(headers={"Authorization": f"Bearer {token}"})
        resp = http.post(
            f"{url}/v1/publish",
            json={
                "channel": "_system:users",
                "sender": "system",
                "msg_type": "admin-broadcast",
                "payload": "system message",
            },
        )
        assert resp.status_code == 200, (
            f"Expected 200 for supertoken writing to _system:users, got {resp.status_code}"
        )
        http.close()

    def test_regular_agent_can_still_write_to_own_cursors(self, auth_server) -> None:
        """Scoped agent should still write to _system:cursors:{own_id}."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store = auth_server
        token_entry = store.create_token("alice", "Alice's token")
        token = token_entry["token"]

        http = HttpClient(headers={"Authorization": f"Bearer {token}"})
        resp = http.post(
            f"{url}/v1/publish",
            json={
                "channel": "_system:cursors:alice",
                "sender": "alice",
                "msg_type": "cursor",
                "payload": "pos:42",
            },
        )
        assert resp.status_code == 200, (
            f"Expected 200 for agent writing to own cursors channel, got {resp.status_code}"
        )
        http.close()

    def test_regular_agent_cannot_write_to_registry(self, auth_server) -> None:
        """Scoped agent cannot write to _system:registry (removed)."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store = auth_server
        token_entry = store.create_token("bob", "Bob's token")
        token = token_entry["token"]

        http = HttpClient(headers={"Authorization": f"Bearer {token}"})
        resp = http.post(
            f"{url}/v1/publish",
            json={
                "channel": "_system:registry",
                "sender": "bob",
                "msg_type": "register",
                "payload": "registering",
            },
        )
        assert resp.status_code == 403, (
            f"Expected 403 for agent writing to _system:registry, got {resp.status_code}"
        )
        http.close()
