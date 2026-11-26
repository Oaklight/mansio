"""Deleting plain channels with a scoped token (issue #367).

``delete_channel_denial`` used to deny every non-private-prefix channel, even
to its owner, while ``Bus.check_access`` reported ``delete`` as allowed for
that owner.  Plain and registered-prefix channels now consult the channel
store: a stored row plus the ``delete`` verb (owner, or an ACL entry granting
``delete``/``manage``) is required.  Unregistered channels and ``broadcast:*``
keep the shared-channel denial; name-private channels and ``_system:*`` are
unchanged.
"""

from __future__ import annotations

import http.client
import json
import urllib.parse

import pytest


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
        for user in ("alice", "bob", "eve")
    }


def _create(url: str, token: str, name: str, visibility: str = "public") -> None:
    status, data = _req(
        f"{url}/v1/channels", "POST", token, {"name": name, "visibility": visibility}
    )
    assert status == 201, data


def _publish(url: str, token: str | None, sender: str, channel: str, payload: str = "hi") -> None:
    status, data = _req(
        f"{url}/v1/publish",
        "POST",
        token,
        {"channel": channel, "sender": sender, "msg_type": "chat", "payload": payload},
    )
    assert status == 200, data


def _delete(url: str, token: str | None, channel: str):
    return _req(f"{url}/v1/channels/{channel}", "DELETE", token)


SHARED_DENIAL = "Scoped tokens cannot delete shared channels"


class TestOwnerCanDelete:
    @pytest.mark.parametrize("visibility", ["public", "private"])
    def test_owner_deletes_plain_channel(self, mansio_server, visibility: str) -> None:
        url, store, bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "team", visibility=visibility)
        _publish(url, tok["alice"], "alice", "team")

        status, data = _delete(url, tok["alice"], "team")

        assert status == 200, data
        assert data == {"deleted": 1, "channel": "team"}
        assert bus.get_channel_meta("team") is None

    def test_acl_owner_preset_can_delete(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "team")
        _publish(url, tok["alice"], "alice", "team")
        status, data = _req(
            f"{url}/v1/channels/team/acl",
            "POST",
            tok["alice"],
            {"user_id": "bob", "permission": "owner"},
        )
        assert status == 201, data

        status, data = _delete(url, tok["bob"], "team")
        assert status == 200, data

    def test_acl_delete_verb_can_delete(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "team")
        _publish(url, tok["alice"], "alice", "team")
        status, data = _req(
            f"{url}/v1/channels/team/acl",
            "POST",
            tok["alice"],
            {"user_id": "bob", "permission": "delete"},
        )
        assert status == 201, data

        status, data = _delete(url, tok["bob"], "team")
        assert status == 200, data


class TestNonOwnerIsDenied:
    def test_member_and_stranger_keep_the_existing_denial(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "team", visibility="private")
        _publish(url, tok["alice"], "alice", "team")
        status, data = _req(
            f"{url}/v1/channels/team/acl",
            "POST",
            tok["alice"],
            {"user_id": "bob", "permission": "member"},
        )
        assert status == 201, data

        for user in ("bob", "eve"):
            status, data = _delete(url, tok[user], "team")
            assert status == 403, data
            assert data["message"] == SHARED_DENIAL

        # Nothing was removed.
        assert bus.get_channel_meta("team") is not None

    def test_unregistered_plain_channel_stays_denied(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        tok = _tokens(store)
        _publish(url, tok["alice"], "alice", "work")
        assert bus.get_channel_meta("work") is None

        status, data = _delete(url, tok["alice"], "work")
        assert status == 403, data
        assert data["message"] == SHARED_DENIAL

    def test_broadcast_owner_stays_denied(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        tok = _tokens(store)
        _publish(url, tok["alice"], "alice", "broadcast:news")
        # First publisher owns the auto-created row, but delete stays closed.
        assert bus.get_channel_meta("broadcast:news") is not None

        status, data = _delete(url, tok["alice"], "broadcast:news")
        assert status == 403, data
        assert data["message"] == SHARED_DENIAL


class TestUnchangedRules:
    def test_dm_participant_still_deletes(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _publish(url, tok["alice"], "alice", "dm:alice:bob")

        status, data = _delete(url, tok["bob"], "dm:alice:bob")
        assert status == 200, data

    def test_supertoken_still_deletes(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        _create(url, tok["alice"], "team", visibility="private")
        _publish(url, tok["alice"], "alice", "team")
        supertoken = store.create_token(user_id=None, label="super")["token"]

        status, data = _delete(url, supertoken, "team")
        assert status == 200, data

    def test_no_auth_still_deletes(self, server_url) -> None:
        status, data = _req(
            f"{server_url}/v1/channels",
            "POST",
            None,
            {"name": "team", "owner": "alice", "visibility": "private"},
        )
        assert status == 201, data
        _publish(server_url, None, "alice", "team")

        status, data = _delete(server_url, None, "team")
        assert status == 200, data
