"""Authorization for ``POST /v1/channels`` (issue #358).

Creating a channel used to check only that the name was non-empty (and, since
#351, that a ``dm:`` name was well formed).  Any scoped token could therefore
create a metadata row in another agent's namespace.  The handler now applies
the same structural rules as publish: ``notebook:X`` / ``memory:X`` only for
``X``, ``dm:<a>:<b>`` only for ``a`` or ``b``, and ``_system:*`` never for a
scoped token.  Supertokens and no-auth mode keep bypassing the check.
"""

from __future__ import annotations

import http.client
import json
import urllib.parse

import pytest
from mansio_core import Bus, MemoryBackend
from mansio_core.authz import ChannelAuthorizer


def _req(url: str, method: str = "GET", token: str | None = None, body: dict | None = None):
    parsed = urllib.parse.urlparse(url)
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    raw = json.dumps(body).encode() if body is not None else None
    headers: dict[str, str] = {}
    if raw is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, target, body=raw, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode() or "{}")
    status = resp.status
    conn.close()
    return status, data


def _tokens(store) -> dict[str, str]:
    return {
        user: store.create_token(user_id=user, label=f"{user} test token")["token"]
        for user in ("alice", "bob", "eve")
    }


def _create(url: str, token: str | None, name: str, **extra) -> tuple[int, dict]:
    body = {"name": name, **extra}
    return _req(f"{url}/v1/channels", "POST", token, body)


def _publish(url: str, token: str, user: str, channel: str) -> tuple[int, dict]:
    body = {"channel": channel, "sender": user, "msg_type": "text", "payload": "hello"}
    return _req(f"{url}/v1/publish", "POST", token, body)


class TestScopedTokenIsDenied:
    @pytest.mark.parametrize("channel", ["notebook:bob", "memory:bob"])
    def test_stranger_cannot_create_another_agents_namespace(self, mansio_server, channel) -> None:
        url, store, bus, _server = mansio_server
        tokens = _tokens(store)

        status, data = _create(url, tokens["eve"], channel)

        assert status == 403, data
        assert data["message"] == f"Channel '{channel}' belongs to agent 'bob'"
        assert bus.get_channel_meta(channel) is None

    def test_stranger_cannot_create_another_agents_dm(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        tokens = _tokens(store)

        status, data = _create(url, tokens["eve"], "dm:alice:bob", visibility="private")

        assert status == 403, data
        assert data["message"] == "Channel 'dm:alice:bob' is private"
        assert bus.get_channel_meta("dm:alice:bob") is None

    def test_scoped_token_cannot_create_system_channel(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        tokens = _tokens(store)

        status, data = _create(url, tokens["eve"], "_system:ops")

        assert status == 403, data
        assert data["message"] == "Channel '_system:ops' is reserved for system use"
        assert bus.get_channel_meta("_system:ops") is None

    def test_scoped_token_cannot_create_broadcast_topic(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        tokens = _tokens(store)

        status, data = _create(url, tokens["eve"], "broadcast:news")

        assert status == 403, data
        assert data["message"] == ("Channel 'broadcast:news' is reserved for its first publisher")
        assert bus.get_channel_meta("broadcast:news") is None

    def test_denied_create_then_first_publish_claims_the_topic(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        tokens = _tokens(store)

        assert _create(url, tokens["eve"], "broadcast:news")[0] == 403

        status, data = _publish(url, tokens["eve"], "eve", "broadcast:news")

        assert status == 200, data
        meta = bus.get_channel_meta("broadcast:news")
        assert meta is not None
        assert meta.owner == "eve"

    def test_malformed_dm_stays_a_client_error(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tokens = _tokens(store)

        status, data = _create(url, tokens["alice"], "dm:alice")

        assert status == 400, data
        assert "Invalid dm channel name" in data["message"]


class TestScopedTokenIsAllowed:
    def test_owner_can_create_own_namespace(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        tokens = _tokens(store)

        for channel in ("notebook:bob", "memory:bob"):
            status, data = _create(url, tokens["bob"], channel, visibility="private")
            assert status == 201, data
            assert data["channel"]["owner"] == "bob"
            assert bus.get_channel_meta(channel) is not None

    def test_dm_participants_can_create(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tokens = _tokens(store)

        for user, channel in (("alice", "dm:alice:bob"), ("bob", "dm:bob:alice")):
            status, data = _create(url, tokens[user], channel, visibility="private")
            assert status == 201, data
            assert data["channel"]["owner"] == user

    def test_plain_channel_creation_stays_open(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tokens = _tokens(store)

        status, data = _create(url, tokens["eve"], "team")

        assert status == 201, data
        assert data["channel"]["owner"] == "eve"


class TestUnscopedCallersBypass:
    def test_supertoken_can_create_reserved_names(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        supertoken = store.create_token(user_id=None, label="super")["token"]

        for channel in (
            "notebook:bob",
            "memory:bob",
            "dm:alice:bob",
            "_system:ops",
            "broadcast:news",
        ):
            status, data = _create(url, supertoken, channel, owner="operator", visibility="private")
            assert status == 201, data
            assert data["channel"]["owner"] == "operator"
            assert bus.get_channel_meta(channel) is not None

    def test_pre_created_broadcast_keeps_its_owner(self, mansio_server) -> None:
        """A supertoken may pre-create a topic; a scoped publish by a
        non-owner is still denied, so ownership is not reassigned."""
        url, store, bus, _server = mansio_server
        tokens = _tokens(store)
        supertoken = store.create_token(user_id=None, label="super")["token"]

        status, data = _create(url, supertoken, "broadcast:news", owner="operator")
        assert status == 201, data

        status, data = _publish(url, tokens["alice"], "alice", "broadcast:news")

        assert status == 403, data
        meta = bus.get_channel_meta("broadcast:news")
        assert meta is not None
        assert meta.owner == "operator"

    def test_no_auth_mode_can_create_reserved_names(self, server_url) -> None:
        for channel in ("notebook:alice", "dm:alice:bob", "_system:ops", "broadcast:news"):
            status, data = _create(server_url, None, channel, owner="alice")
            assert status == 201, data
            assert data["channel"]["owner"] == "alice"


class TestRuleParityWithPublish:
    """Create and publish must agree on the structural private namespaces."""

    @pytest.mark.parametrize(
        "channel,user",
        [
            ("notebook:bob", "bob"),
            ("notebook:bob", "eve"),
            ("memory:bob", "bob"),
            ("memory:bob", "eve"),
            ("dm:alice:bob", "alice"),
            ("dm:alice:bob", "bob"),
            ("dm:alice:bob", "eve"),
        ],
    )
    def test_create_and_publish_agree(self, channel, user) -> None:
        bus = Bus(backend=MemoryBackend())
        authorizer = ChannelAuthorizer(bus)

        assert authorizer.can_create_channel(user, channel) == authorizer.can_publish(user, channel)

    def test_broadcast_creation_is_stricter_than_publish(self) -> None:
        """Publish auto-creates the topic with the publisher as owner, so a
        scoped token may claim it that way; creating it directly may not."""
        bus = Bus(backend=MemoryBackend())
        authorizer = ChannelAuthorizer(bus)

        assert authorizer.can_publish("eve", "broadcast:news")
        assert not authorizer.can_create_channel("eve", "broadcast:news")
