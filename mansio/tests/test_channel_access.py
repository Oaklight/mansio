"""Tests for structural channel ownership parsing — issues #249 and #251.

Verifies that _is_channel_member uses structural parsing (not substring
matching) and that the query endpoint blocks unauthorized private channel
access before leaking the total message count.
"""

from __future__ import annotations

import threading
import time

from mansio import Bus, MansioServer, MemoryBackend
from mansio.frontends.http import HttpFrontend, _is_channel_member

# ──────────────────────────────────────────────
# Unit tests for _is_channel_member (#249)
# ──────────────────────────────────────────────


class TestIsChannelMember:
    """_is_channel_member must use structural parsing, not substring match."""

    def test_notebook_owner_matches(self) -> None:
        assert _is_channel_member("alice", "notebook:alice") is True

    def test_memory_owner_matches(self) -> None:
        assert _is_channel_member("bob", "memory:bob") is True

    def test_dm_participant_a_matches(self) -> None:
        assert _is_channel_member("alice", "dm:alice:bob") is True

    def test_dm_participant_b_matches(self) -> None:
        assert _is_channel_member("bob", "dm:alice:bob") is True

    # --- False-positive regressions (#249) ---

    def test_notebook_prefix_does_not_match_as_user(self) -> None:
        """User 'notebook' must NOT match channel 'notebook:alice'."""
        assert _is_channel_member("notebook", "notebook:alice") is False

    def test_memory_prefix_does_not_match_as_user(self) -> None:
        """User 'memory' must NOT match channel 'memory:bob'."""
        assert _is_channel_member("memory", "memory:bob") is False

    def test_dm_prefix_does_not_match_as_user(self) -> None:
        """User 'dm' must NOT match channel 'dm:alice:bob'."""
        assert _is_channel_member("dm", "dm:alice:bob") is False

    def test_unrelated_user_does_not_match_notebook(self) -> None:
        assert _is_channel_member("eve", "notebook:alice") is False

    def test_unrelated_user_does_not_match_dm(self) -> None:
        assert _is_channel_member("eve", "dm:alice:bob") is False

    def test_substring_user_does_not_match_notebook(self) -> None:
        """User 'ali' must NOT match channel 'notebook:alice'."""
        assert _is_channel_member("ali", "notebook:alice") is False

    def test_public_channel_returns_false(self) -> None:
        """Non-private channels always return False (not a member concept)."""
        assert _is_channel_member("alice", "general") is False


# ──────────────────────────────────────────────
# Integration tests for query endpoint (#251)
# ──────────────────────────────────────────────


def _make_auth_server(tmp_path):
    """Start a MansioServer with token auth, return (url, token_store, server, bus)."""
    from mansio.token_store import TokenStore

    db_path = str(tmp_path / "channel_access_test.db")
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


class TestQueryPrivateChannelAccess:
    """GET /v1/query must return 403 for unauthorized private channel access."""

    def test_query_others_notebook_returns_403(self, tmp_path) -> None:
        """Scoped token cannot query another user's notebook channel (#251)."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store, server, bus = _make_auth_server(tmp_path)
        try:
            alice_token = store.create_token("alice", "Alice")["token"]
            eve_token = store.create_token("eve", "Eve")["token"]

            # Populate alice's private notebook
            bus.publish("notebook:alice", "alice", "note", "secret note 1")
            bus.publish("notebook:alice", "alice", "note", "secret note 2")
            bus.publish("notebook:alice", "alice", "note", "secret note 3")

            http_eve = HttpClient(headers={"Authorization": f"Bearer {eve_token}"})
            resp = http_eve.get(f"{url}/v1/query?channel=notebook:alice")
            assert resp.status_code == 403
            body = resp.json()
            assert body["error"] == "Forbidden"

            # Alice CAN query her own notebook
            http_alice = HttpClient(headers={"Authorization": f"Bearer {alice_token}"})
            resp = http_alice.get(f"{url}/v1/query?channel=notebook:alice")
            assert resp.status_code == 200
            body = resp.json()
            assert body["count"] == 3
        finally:
            server.shutdown()
            bus.close()

    def test_query_others_dm_returns_403(self, tmp_path) -> None:
        """Scoped token cannot query a DM channel they are not part of."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store, server, bus = _make_auth_server(tmp_path)
        try:
            store.create_token("alice", "Alice")
            store.create_token("bob", "Bob")
            eve_token = store.create_token("eve", "Eve")["token"]

            bus.publish("dm:alice:bob", "alice", "chat", "private msg")

            http_eve = HttpClient(headers={"Authorization": f"Bearer {eve_token}"})
            resp = http_eve.get(f"{url}/v1/query?channel=dm:alice:bob")
            assert resp.status_code == 403
        finally:
            server.shutdown()
            bus.close()

    def test_query_total_not_leaked(self, tmp_path) -> None:
        """403 response must not contain the total message count (#251)."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store, server, bus = _make_auth_server(tmp_path)
        try:
            store.create_token("alice", "Alice")
            eve_token = store.create_token("eve", "Eve")["token"]

            for i in range(5):
                bus.publish("notebook:alice", "alice", "note", f"note {i}")

            http_eve = HttpClient(headers={"Authorization": f"Bearer {eve_token}"})
            resp = http_eve.get(f"{url}/v1/query?channel=notebook:alice")
            assert resp.status_code == 403
            body = resp.json()
            # The response must NOT contain a "total" key
            assert "total" not in body
        finally:
            server.shutdown()
            bus.close()

    def test_supertoken_can_query_any_channel(self, tmp_path) -> None:
        """Supertokens (user_id=None) bypass private channel checks."""
        from mansio._vendor.httpclient import Client as HttpClient

        url, store, server, bus = _make_auth_server(tmp_path)
        try:
            super_token = store.create_token(user_id=None, label="admin")["token"]
            store.create_token("alice", "Alice")

            bus.publish("notebook:alice", "alice", "note", "secret")

            http_admin = HttpClient(headers={"Authorization": f"Bearer {super_token}"})
            resp = http_admin.get(f"{url}/v1/query?channel=notebook:alice")
            assert resp.status_code == 200
        finally:
            server.shutdown()
            bus.close()
