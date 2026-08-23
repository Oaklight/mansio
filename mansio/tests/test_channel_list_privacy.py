"""Tests for channel_list privacy — fix for #104.

Verifies that /v1/channels filters out private channels (DM, notebook,
memory) the authenticated agent is not a participant of.
"""

from __future__ import annotations

import threading
import time

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend


def _make_auth_server(tmp_path):
    """Start a MansioServer with token auth, return (url, token_store, server)."""
    from mansio.token_store import TokenStore

    db_path = str(tmp_path / "channel_list_test.db")
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
    return url, token_store, server, bus


class TestChannelListPrivacy:
    """channel_list must not leak private channels to unrelated agents (#104)."""

    def test_dm_channel_hidden_from_non_participant(self, tmp_path) -> None:
        """Agent should not see DM channels they are not part of."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store, server, bus = _make_auth_server(tmp_path)
        try:
            alice_token = store.create_token("alice", "Alice")["token"]
            bob_token = store.create_token("bob", "Bob")["token"]
            eve_token = store.create_token("eve", "Eve")["token"]

            # Create messages so channels appear in list
            bus.publish("dm:alice:bob", "alice", "chat", "secret")
            bus.publish("dm:alice:eve", "alice", "chat", "hi eve")
            bus.publish("public-chat", "alice", "chat", "hello world")

            http_alice = HttpClient(headers={"Authorization": f"Bearer {alice_token}"})
            http_bob = HttpClient(headers={"Authorization": f"Bearer {bob_token}"})
            http_eve = HttpClient(headers={"Authorization": f"Bearer {eve_token}"})

            # Alice is in both DMs — should see both
            resp = http_alice.get(f"{url}/v1/channels")
            assert resp.status_code == 200
            alice_channels = resp.json()["channels"]
            assert "dm:alice:bob" in alice_channels
            assert "dm:alice:eve" in alice_channels
            assert "public-chat" in alice_channels

            # Bob is only in dm:alice:bob — should NOT see dm:alice:eve
            resp = http_bob.get(f"{url}/v1/channels")
            assert resp.status_code == 200
            bob_channels = resp.json()["channels"]
            assert "dm:alice:bob" in bob_channels
            assert "dm:alice:eve" not in bob_channels, "Bob should not see dm:alice:eve"
            assert "public-chat" in bob_channels

            # Eve is only in dm:alice:eve — should NOT see dm:alice:bob
            resp = http_eve.get(f"{url}/v1/channels")
            assert resp.status_code == 200
            eve_channels = resp.json()["channels"]
            assert "dm:alice:eve" in eve_channels
            assert "dm:alice:bob" not in eve_channels, "Eve should not see dm:alice:bob"
            assert "public-chat" in eve_channels

            http_alice.close()
            http_bob.close()
            http_eve.close()
        finally:
            server.shutdown()

    def test_notebook_channel_hidden_from_other_agents(self, tmp_path) -> None:
        """Agent should not see another agent's notebook channel."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store, server, bus = _make_auth_server(tmp_path)
        try:
            milo_token = store.create_token("milo", "Milo")["token"]
            elena_token = store.create_token("elena", "Elena")["token"]

            bus.publish("notebook:milo", "milo", "note", "private note")
            bus.publish("notebook:elena", "elena", "note", "my note")
            bus.publish("general", "milo", "chat", "public msg")

            http_milo = HttpClient(headers={"Authorization": f"Bearer {milo_token}"})
            http_elena = HttpClient(headers={"Authorization": f"Bearer {elena_token}"})

            resp = http_milo.get(f"{url}/v1/channels")
            milo_channels = resp.json()["channels"]
            assert "notebook:milo" in milo_channels
            assert "notebook:elena" not in milo_channels, "Milo should not see Elena's notebook"
            assert "general" in milo_channels

            resp = http_elena.get(f"{url}/v1/channels")
            elena_channels = resp.json()["channels"]
            assert "notebook:elena" in elena_channels
            assert "notebook:milo" not in elena_channels, "Elena should not see Milo's notebook"
            assert "general" in elena_channels

            http_milo.close()
            http_elena.close()
        finally:
            server.shutdown()

    def test_memory_channel_hidden_from_other_agents(self, tmp_path) -> None:
        """Agent should not see another agent's memory channel."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store, server, bus = _make_auth_server(tmp_path)
        try:
            alice_token = store.create_token("alice", "Alice")["token"]
            bob_token = store.create_token("bob", "Bob")["token"]

            bus.publish("memory:alice", "alice", "memory", "my memories")
            bus.publish("memory:bob", "bob", "memory", "bob memories")

            http_alice = HttpClient(headers={"Authorization": f"Bearer {alice_token}"})
            http_bob = HttpClient(headers={"Authorization": f"Bearer {bob_token}"})

            resp = http_alice.get(f"{url}/v1/channels")
            alice_channels = resp.json()["channels"]
            assert "memory:alice" in alice_channels
            assert "memory:bob" not in alice_channels, "Alice should not see Bob's memory channel"

            resp = http_bob.get(f"{url}/v1/channels")
            bob_channels = resp.json()["channels"]
            assert "memory:bob" in bob_channels
            assert "memory:alice" not in bob_channels, "Bob should not see Alice's memory channel"

            http_alice.close()
            http_bob.close()
        finally:
            server.shutdown()

    def test_public_channels_visible_to_all(self, tmp_path) -> None:
        """Public and _system channels should be visible to all agents."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store, server, bus = _make_auth_server(tmp_path)
        try:
            a_token = store.create_token("agent-a", "A")["token"]
            b_token = store.create_token("agent-b", "B")["token"]

            bus.publish("dev-mansio", "agent-a", "chat", "hello")
            bus.publish("_system:agents", "agent-a", "system", "heartbeat")
            bus.publish("broadcast:news", "agent-a", "chat", "news")

            for token in [a_token, b_token]:
                http = HttpClient(headers={"Authorization": f"Bearer {token}"})
                resp = http.get(f"{url}/v1/channels")
                channels = resp.json()["channels"]
                assert "dev-mansio" in channels
                assert "_system:agents" in channels
                assert "broadcast:news" in channels
                http.close()
        finally:
            server.shutdown()

    def test_no_auth_returns_all_channels(self, tmp_path) -> None:
        """Without token auth, all channels are returned (backward compat)."""
        from mansio._vendor.httpclient import Client as HttpClient

        bus = Bus(backend=MemoryBackend())
        frontend = HttpFrontend(host="127.0.0.1", port=0)
        server = MansioServer(bus)
        server.add_frontend(frontend)

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.3)

        host, port = frontend.address
        url = f"http://{host}:{port}"

        try:
            bus.publish("dm:alice:bob", "alice", "chat", "dm")
            bus.publish("public", "alice", "chat", "public")

            http = HttpClient()
            resp = http.get(f"{url}/v1/channels")
            channels = resp.json()["channels"]
            assert "dm:alice:bob" in channels
            assert "public" in channels
            http.close()
        finally:
            server.shutdown()
