"""Tests for channel_list filtering of private channels (issue #104).

Verifies:
- Authenticated agents only see private channels they're involved in
- Public channels are visible to all authenticated agents
- DM channels are filtered based on agent involvement
- notebook: and memory: channels are filtered based on ownership
- Supertokens see all channels
- No-auth mode shows all channels
"""

from __future__ import annotations

import threading
import time

import pytest

from piazza import Bus, MemoryBackend, PiazzaServer
from piazza.frontends import HttpFrontend
from piazza.transport_http import HttpTransport


@pytest.fixture()
def auth_server(tmp_path):
    """Start a PiazzaServer with token auth enabled, yield (url, token_store, bus)."""
    from piazza.token_store import TokenStore

    db_path = str(tmp_path / "channel_filter_test.db")
    token_store = TokenStore(db_path)

    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(host="127.0.0.1", port=0, token_store=token_store)
    server = PiazzaServer(bus)
    server.add_frontend(frontend)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    host, port = frontend.address
    url = f"http://{host}:{port}"

    yield url, token_store, bus

    server.shutdown()


class TestChannelListFiltering:
    """Channel list should filter out private channels for non-involved agents."""

    def test_dm_channel_hidden_from_non_participant(self, auth_server) -> None:
        """An agent should not see DM channels they're not part of."""
        url, store, bus = auth_server

        alice_token = store.create_token("alice", "Alice")["token"]
        bob_token = store.create_token("bob", "Bob")["token"]
        eve_token = store.create_token("eve", "Eve")["token"]

        alice_t = HttpTransport(url, agent_id="alice", token=alice_token)
        bob_t = HttpTransport(url, agent_id="bob", token=bob_token)
        eve_t = HttpTransport(url, agent_id="eve", token=eve_token)

        # Alice sends a DM to Bob — creates the dm:alice:bob channel
        alice_t.publish("dm:alice:bob", "alice", "chat", "secret message")

        # Alice should see the DM channel
        from piazza._vendor.httpclient import Client as HttpClient

        http_alice = HttpClient(headers={"Authorization": f"Bearer {alice_token}"})
        resp = http_alice.get(f"{url}/v1/channels")
        assert resp.status_code == 200
        channels = resp.json()["channels"]
        assert "dm:alice:bob" in channels, "Alice should see her own DM channel"
        http_alice.close()

        # Bob should see the DM channel
        http_bob = HttpClient(headers={"Authorization": f"Bearer {bob_token}"})
        resp = http_bob.get(f"{url}/v1/channels")
        channels = resp.json()["channels"]
        assert "dm:alice:bob" in channels, "Bob should see the DM channel he's part of"
        http_bob.close()

        # Eve should NOT see the DM channel
        http_eve = HttpClient(headers={"Authorization": f"Bearer {eve_token}"})
        resp = http_eve.get(f"{url}/v1/channels")
        channels = resp.json()["channels"]
        assert "dm:alice:bob" not in channels, "Eve should not see a DM she's not part of"
        http_eve.close()

        alice_t.close()
        bob_t.close()
        eve_t.close()

    def test_notebook_channel_hidden_from_non_owner(self, auth_server) -> None:
        """notebook: channels should only be visible to their owner."""
        url, store, bus = auth_server

        milo_token = store.create_token("milo", "Milo")["token"]
        elena_token = store.create_token("elena", "Elena")["token"]

        milo_t = HttpTransport(url, agent_id="milo", token=milo_token)
        milo_t.publish("notebook:milo", "milo", "note", "private note")

        from piazza._vendor.httpclient import Client as HttpClient

        # Milo sees his notebook
        http_milo = HttpClient(headers={"Authorization": f"Bearer {milo_token}"})
        resp = http_milo.get(f"{url}/v1/channels")
        channels = resp.json()["channels"]
        assert "notebook:milo" in channels
        http_milo.close()

        # Elena does NOT see Milo's notebook
        http_elena = HttpClient(headers={"Authorization": f"Bearer {elena_token}"})
        resp = http_elena.get(f"{url}/v1/channels")
        channels = resp.json()["channels"]
        assert "notebook:milo" not in channels, "Elena should not see Milo's notebook"
        http_elena.close()

        milo_t.close()

    def test_memory_channel_hidden_from_non_owner(self, auth_server) -> None:
        """memory: channels should only be visible to their owner."""
        url, store, bus = auth_server

        agent_token = store.create_token("agent-x", "Agent X")["token"]
        other_token = store.create_token("agent-y", "Agent Y")["token"]

        agent_t = HttpTransport(url, agent_id="agent-x", token=agent_token)
        agent_t.publish("memory:agent-x", "agent-x", "memory", "private memory")

        from piazza._vendor.httpclient import Client as HttpClient

        # Owner sees it
        http_own = HttpClient(headers={"Authorization": f"Bearer {agent_token}"})
        resp = http_own.get(f"{url}/v1/channels")
        channels = resp.json()["channels"]
        assert "memory:agent-x" in channels
        http_own.close()

        # Other agent does NOT see it
        http_other = HttpClient(headers={"Authorization": f"Bearer {other_token}"})
        resp = http_other.get(f"{url}/v1/channels")
        channels = resp.json()["channels"]
        assert "memory:agent-x" not in channels
        http_other.close()

        agent_t.close()

    def test_public_channels_visible_to_all(self, auth_server) -> None:
        """Public channels should be visible to all authenticated agents."""
        url, store, bus = auth_server

        alice_token = store.create_token("alice", "Alice")["token"]
        bob_token = store.create_token("bob", "Bob")["token"]

        alice_t = HttpTransport(url, agent_id="alice", token=alice_token)
        alice_t.publish("dev-piazza", "alice", "chat", "hello")

        from piazza._vendor.httpclient import Client as HttpClient

        # Both agents see the public channel
        for token_val, name in [(alice_token, "alice"), (bob_token, "bob")]:
            http = HttpClient(headers={"Authorization": f"Bearer {token_val}"})
            resp = http.get(f"{url}/v1/channels")
            channels = resp.json()["channels"]
            assert "dev-piazza" in channels, f"{name} should see public channel"
            http.close()

        alice_t.close()

    def test_supertoken_sees_all_channels(self, auth_server) -> None:
        """Supertokens (agent_id=None) should see all channels."""
        url, store, bus = auth_server

        alice_token = store.create_token("alice", "Alice")["token"]
        super_token = store.create_token(agent_id=None, label="Supertoken")["token"]

        alice_t = HttpTransport(url, agent_id="alice", token=alice_token)
        alice_t.publish("dm:alice:bob", "alice", "chat", "dm message")
        alice_t.publish("dev-piazza", "alice", "chat", "public message")

        from piazza._vendor.httpclient import Client as HttpClient

        http = HttpClient(headers={"Authorization": f"Bearer {super_token}"})
        resp = http.get(f"{url}/v1/channels")
        channels = resp.json()["channels"]
        assert "dm:alice:bob" in channels, "Supertoken should see DM channels"
        assert "dev-piazza" in channels, "Supertoken should see public channels"
        http.close()

        alice_t.close()

    def test_no_auth_shows_all_channels(self, server_url: str) -> None:
        """Without auth, all channels should be visible."""
        from piazza._vendor.httpclient import Client as HttpClient
        from piazza import PiazzaClient

        client = PiazzaClient(server_url, "tester")
        client.channel_send("test-public", "hello")
        client.close()

        http = HttpClient()
        resp = http.get(f"{server_url}/v1/channels")
        channels = resp.json()["channels"]
        assert "test-public" in channels
        http.close()
