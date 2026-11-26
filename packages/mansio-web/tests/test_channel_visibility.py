"""Plain-channel visibility and ACLs over HTTP (issue #356).

``visibility="private"`` was stored but never enforced: after #343/#347 the
HTTP policy only looked at the channel name, so any scoped token could read
and publish to a private plain channel.  ``ChannelAuthorizer`` now asks the
channel store (``Bus.check_access``) for plain and registered-prefix
channels, which applies owner, visibility, and ACL entries.
"""

from __future__ import annotations

import http.client
import json
import urllib.parse

import pytest
from mansio_core import Bus, MemoryBackend
from mansio_core.authz import ChannelAuthorizer


def _req(url: str, method: str, token: str | None = None, body: dict | None = None):
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
        for user in ("alice", "bob", "carol", "eve")
    }


def _create(url: str, token: str, name: str, visibility: str) -> None:
    status, data = _req(
        f"{url}/v1/channels", "POST", token, {"name": name, "visibility": visibility}
    )
    assert status == 201, data


def _publish(url: str, token: str, sender: str, channel: str) -> int:
    status, _data = _req(
        f"{url}/v1/publish",
        "POST",
        token,
        {"channel": channel, "sender": sender, "msg_type": "chat", "payload": "hi"},
    )
    return status


def _add_acl(url: str, token: str, channel: str, user: str, permission: str) -> None:
    status, data = _req(
        f"{url}/v1/channels/{channel}/acl",
        "POST",
        token,
        {"user_id": user, "permission": permission},
    )
    assert status == 201, data


def _channels(url: str, token: str, detail: bool = False) -> list:
    suffix = "?detail=true" if detail else ""
    status, data = _req(f"{url}/v1/channels{suffix}", "GET", token)
    assert status == 200, data
    return data["channels"]


class TestPrivatePlainChannel:
    def test_owner_has_full_access(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "ops", "private")

        assert _publish(url, tok["alice"], "alice", "ops") == 200

        status, data = _req(f"{url}/v1/query?channel=ops", "GET", tok["alice"])
        assert status == 200, data
        assert data["count"] == 1

        status, _data = _req(f"{url}/v1/channels/ops/meta", "GET", tok["alice"])
        assert status == 200
        assert "ops" in _channels(url, tok["alice"])
        assert any(ch["name"] == "ops" for ch in _channels(url, tok["alice"], detail=True))

    def test_stranger_is_denied_everywhere(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "ops", "private")
        assert _publish(url, tok["alice"], "alice", "ops") == 200

        assert _publish(url, tok["eve"], "eve", "ops") == 403

        status, data = _req(f"{url}/v1/query?channel=ops", "GET", tok["eve"])
        assert status == 403, data

        status, _data = _req(f"{url}/v1/channels/ops/meta", "GET", tok["eve"])
        assert status == 404

        assert "ops" not in _channels(url, tok["eve"])
        assert not any(ch["name"] == "ops" for ch in _channels(url, tok["eve"], detail=True))

    def test_acl_viewer_reads_but_cannot_publish(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "ops", "private")
        assert _publish(url, tok["alice"], "alice", "ops") == 200
        _add_acl(url, tok["alice"], "ops", "bob", "viewer")

        status, data = _req(f"{url}/v1/query?channel=ops", "GET", tok["bob"])
        assert status == 200, data
        assert data["count"] == 1

        assert _publish(url, tok["bob"], "bob", "ops") == 403

        status, _data = _req(f"{url}/v1/channels/ops/meta", "GET", tok["bob"])
        assert status == 200
        assert "ops" in _channels(url, tok["bob"])

    def test_acl_member_can_publish(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "ops", "private")
        _add_acl(url, tok["alice"], "ops", "carol", "member")

        assert _publish(url, tok["carol"], "carol", "ops") == 200

    def test_queue_claim_uses_the_same_rule(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "ops", "private")
        status, _data = _req(
            f"{url}/v1/publish",
            "POST",
            tok["alice"],
            {
                "channel": "ops",
                "sender": "alice",
                "msg_type": "task",
                "payload": "private task",
                "queue": True,
            },
        )
        assert status == 200

        status, data = _req(f"{url}/v1/queue/claim", "POST", tok["eve"], {"channel": "ops"})
        assert status == 403, data
        assert "cannot publish" in data["message"]


class TestPublicPlainChannel:
    def test_unchanged_for_any_token(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "team", "public")

        assert _publish(url, tok["eve"], "eve", "team") == 200
        status, data = _req(f"{url}/v1/query?channel=team", "GET", tok["eve"])
        assert status == 200, data
        status, _data = _req(f"{url}/v1/channels/team/meta", "GET", tok["eve"])
        assert status == 200
        assert "team" in _channels(url, tok["eve"])


class TestSuperTokenAndNoAuth:
    def test_supertoken_reads_private_plain_channel(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "ops", "private")
        assert _publish(url, tok["alice"], "alice", "ops") == 200
        supertoken = store.create_token(user_id=None, label="super")["token"]

        status, data = _req(f"{url}/v1/query?channel=ops", "GET", supertoken)
        assert status == 200, data

    def test_no_auth_reads_private_plain_channel(self, server_url) -> None:
        status, data = _req(
            f"{server_url}/v1/channels",
            "POST",
            None,
            {"name": "ops", "owner": "alice", "visibility": "private"},
        )
        assert status == 201, data
        status, _data = _req(
            f"{server_url}/v1/publish",
            "POST",
            None,
            {"channel": "ops", "sender": "alice", "msg_type": "chat", "payload": "hi"},
        )
        assert status == 200

        status, data = _req(f"{server_url}/v1/query?channel=ops", "GET")
        assert status == 200, data


class TestAuthorizerStoreDelegation:
    def test_store_decides_plain_channels(self) -> None:
        bus = Bus(MemoryBackend())
        try:
            bus.create_channel("ops", "alice", visibility="private")
            from mansio_core.types import ACLEntry

            bus.add_acl_entry(ACLEntry(channel="ops", user_id="reader", permission="viewer"))
            bus.add_acl_entry(ACLEntry(channel="ops", user_id="writer", permission="member"))
            authz = ChannelAuthorizer(bus)

            assert authz.can_read("alice", "ops")
            assert authz.can_publish("alice", "ops")
            assert authz.can_read("reader", "ops")
            assert not authz.can_publish("reader", "ops")
            assert authz.can_publish("writer", "ops")
            assert not authz.can_read("eve", "ops")
            assert not authz.can_publish("eve", "ops")
            denial = authz.publish_denial("eve", "ops")
            assert denial is not None
            assert "cannot publish" in denial
        finally:
            bus.close()

    def test_unregistered_channel_stays_public(self) -> None:
        bus = Bus(MemoryBackend())
        try:
            authz = ChannelAuthorizer(bus)
            assert authz.can_read("eve", "general")
            assert authz.can_publish("eve", "general")
        finally:
            bus.close()

    def test_backend_without_channel_store_stays_open(self, tmp_path) -> None:
        from mansio_core.backends.maildir import MaildirBackend

        bus = Bus(MaildirBackend(tmp_path / "mail"))
        try:
            authz = ChannelAuthorizer(bus)
            assert authz.can_read("eve", "general")
            assert authz.can_publish("eve", "general")
            # A private row cannot be created there in the first place.
            with pytest.raises(NotImplementedError):
                bus.create_channel("ops", "alice", visibility="private")
        finally:
            bus.close()
