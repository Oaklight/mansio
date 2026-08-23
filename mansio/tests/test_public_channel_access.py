"""Tests for public channel access — fixes #92 and #93.

Verifies:
- Query on public channels returns all agents' messages (not just own)
- SSE subscribe to public channels succeeds (no 403)
- DM isolation: agents not in DM can't see messages
- Private channel restrictions (notebook:, memory:) still enforced
"""

from __future__ import annotations

import threading
import time

import pytest

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends import HttpFrontend
from mansio.transport_http import HttpTransport


@pytest.fixture()
def auth_server(tmp_path):
    """Start a MansioServer with token auth enabled, yield (url, token_store)."""
    from mansio.token_store import TokenStore

    db_path = str(tmp_path / "public_ch_test.db")
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


class TestPublicChannelQuery:
    """Query on public channels should return all agents' messages (#92)."""

    def test_query_public_channel_shows_all_agents(self, auth_server) -> None:
        """Two agents post to a public channel; each should see both messages."""
        url, store = auth_server

        alice_token = store.create_token("alice", "Alice's token")["token"]
        bob_token = store.create_token("bob", "Bob's token")["token"]

        alice_t = HttpTransport(url, agent_id="alice", token=alice_token)
        bob_t = HttpTransport(url, agent_id="bob", token=bob_token)

        alice_t.publish("dev-mansio", "alice", "chat", "hello from alice")
        bob_t.publish("dev-mansio", "bob", "chat", "hello from bob")

        # Alice queries — should see both messages
        alice_msgs = alice_t.query("dev-mansio", limit=100)
        alice_senders = {m.sender for m in alice_msgs}
        assert "alice" in alice_senders, "Alice should see her own message"
        assert "bob" in alice_senders, "Alice should see Bob's message on public channel"

        # Bob queries — should also see both messages
        bob_msgs = bob_t.query("dev-mansio", limit=100)
        bob_senders = {m.sender for m in bob_msgs}
        assert "alice" in bob_senders, "Bob should see Alice's message on public channel"
        assert "bob" in bob_senders, "Bob should see his own message"

        alice_t.close()
        bob_t.close()

    def test_query_public_channel_message_count(self, auth_server) -> None:
        """Each agent should get the full message count on a public channel."""
        url, store = auth_server

        a_token = store.create_token("agent-a", "A")["token"]
        b_token = store.create_token("agent-b", "B")["token"]
        c_token = store.create_token("agent-c", "C")["token"]

        a_t = HttpTransport(url, agent_id="agent-a", token=a_token)
        b_t = HttpTransport(url, agent_id="agent-b", token=b_token)
        c_t = HttpTransport(url, agent_id="agent-c", token=c_token)

        a_t.publish("shared-channel", "agent-a", "chat", "msg-a")
        b_t.publish("shared-channel", "agent-b", "chat", "msg-b")
        c_t.publish("shared-channel", "agent-c", "chat", "msg-c")

        # All three agents should see all three messages
        for transport, name in [(a_t, "agent-a"), (b_t, "agent-b"), (c_t, "agent-c")]:
            msgs = transport.query("shared-channel", limit=100)
            assert len(msgs) == 3, f"{name} should see all 3 messages, got {len(msgs)}"

        a_t.close()
        b_t.close()
        c_t.close()


class TestPublicChannelSSE:
    """SSE subscribe to public channels should succeed (#93)."""

    def test_subscribe_public_channel_no_403(self, auth_server) -> None:
        """Subscribing to a public channel should not return 403."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store = auth_server
        token = store.create_token("listener", "Listener token")["token"]

        http = HttpClient(
            headers={"Authorization": f"Bearer {token}"},
        )
        # SSE subscribe — should return 200 with text/event-stream, not 403
        resp = http.get(f"{url}/v1/subscribe?channel=dev-mansio", stream=True)
        assert resp.status_code == 200, (
            f"Expected 200 for public channel SSE, got {resp.status_code}: {resp.text}"
        )
        http.close()

    def test_subscribe_multiple_public_channels(self, auth_server) -> None:
        """Subscribing to multiple public channels at once should succeed."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store = auth_server
        token = store.create_token("multi-sub", "Multi sub token")["token"]

        http = HttpClient(
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = http.get(
            f"{url}/v1/subscribe?channel=channel-a&channel=channel-b",
            stream=True,
        )
        assert resp.status_code == 200, (
            f"Expected 200 for multiple public channels, got {resp.status_code}"
        )
        http.close()


class TestDMIsolation:
    """DM channels must still enforce agent involvement."""

    def test_dm_query_filters_non_participants(self, auth_server) -> None:
        """Agent not in a DM channel should not see its messages."""
        url, store = auth_server

        alice_token = store.create_token("alice", "Alice")["token"]
        bob_token = store.create_token("bob", "Bob")["token"]
        eve_token = store.create_token("eve", "Eve")["token"]

        alice_t = HttpTransport(url, agent_id="alice", token=alice_token)
        bob_t = HttpTransport(url, agent_id="bob", token=bob_token)
        eve_t = HttpTransport(url, agent_id="eve", token=eve_token)

        # Alice sends a DM to Bob
        alice_t.publish("dm:alice:bob", "alice", "chat", "secret for bob")

        # Bob should see it (participant)
        bob_msgs = bob_t.query("dm:alice:bob", limit=100)
        assert len(bob_msgs) == 1, "Bob should see Alice's DM"
        assert bob_msgs[0].payload == "secret for bob"

        # Eve should NOT see it (not a participant)
        eve_msgs = eve_t.query("dm:alice:bob", limit=100)
        assert len(eve_msgs) == 0, "Eve should not see DM between Alice and Bob"

        alice_t.close()
        bob_t.close()
        eve_t.close()

    def test_dm_subscribe_blocked_for_non_participant(self, auth_server) -> None:
        """SSE subscribe to a DM channel should fail for non-participants."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store = auth_server
        token = store.create_token("eve", "Eve's token")["token"]

        http = HttpClient(
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = http.get(f"{url}/v1/subscribe?channel=dm:alice:bob", stream=True)
        assert resp.status_code == 403, (
            f"Expected 403 for non-participant DM subscribe, got {resp.status_code}"
        )
        http.close()


class TestPrivateChannelIsolation:
    """notebook: and memory: channels must still enforce ownership."""

    def test_notebook_query_filtered_for_other_agent(self, auth_server) -> None:
        """Agent should not see messages in another agent's notebook via query."""
        url, store = auth_server

        milo_token = store.create_token("milo", "Milo")["token"]
        elena_token = store.create_token("elena", "Elena")["token"]

        milo_t = HttpTransport(url, agent_id="milo", token=milo_token)
        elena_t = HttpTransport(url, agent_id="elena", token=elena_token)

        milo_t.publish("notebook:milo", "milo", "note", "milo's private note")

        # Milo sees his own note
        milo_msgs = milo_t.query("notebook:milo", limit=100)
        assert len(milo_msgs) == 1

        # Elena should NOT see Milo's note (filtered by _agent_involved)
        elena_msgs = elena_t.query("notebook:milo", limit=100)
        assert len(elena_msgs) == 0, "Elena should not see Milo's notebook messages"

        milo_t.close()
        elena_t.close()

    def test_memory_subscribe_blocked_for_other_agent(self, auth_server) -> None:
        """SSE subscribe to another agent's memory channel should fail."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store = auth_server
        token = store.create_token("snooper", "Snooper")["token"]

        http = HttpClient(
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = http.get(f"{url}/v1/subscribe?channel=memory:target", stream=True)
        assert resp.status_code == 403, (
            f"Expected 403 for non-owner memory subscribe, got {resp.status_code}"
        )
        http.close()

    def test_own_notebook_subscribe_allowed(self, auth_server) -> None:
        """SSE subscribe to own notebook channel should succeed."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store = auth_server
        token = store.create_token("alice", "Alice")["token"]

        http = HttpClient(
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = http.get(f"{url}/v1/subscribe?channel=notebook:alice", stream=True)
        assert resp.status_code == 200, (
            f"Expected 200 for own notebook subscribe, got {resp.status_code}"
        )
        http.close()
