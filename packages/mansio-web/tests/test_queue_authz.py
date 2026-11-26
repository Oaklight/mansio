"""Authorization for the work-queue routes (issue #357).

``POST /v1/queue/claim``, ``POST /v1/queue/ack`` and ``GET /v1/queue/status``
used to resolve the caller identity and nothing else, so a token denied on a
private DM could claim the queued message and receive its payload.

Claim now applies the publish rule; ack and status require read access before
the message or its claim state is returned.  Denials do not confirm that a
private message exists: ``ack`` answers ``{"acked": false}`` like any other
unsuccessful ack, and ``status`` returns the same 404 body as a message with
no queue state.
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


def _publish(url: str, token: str, sender: str, channel: str, payload: str) -> str:
    status, data = _req(
        f"{url}/v1/publish",
        "POST",
        token,
        {
            "channel": channel,
            "sender": sender,
            "msg_type": "task",
            "payload": payload,
            "queue": True,
        },
    )
    assert status == 200, data
    return data["message_id"]


class TestPrivateDmQueue:
    def test_stranger_cannot_claim_or_see_state(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        message_id = _publish(url, tok["alice"], "alice", "dm:alice:bob", "secret-for-bob")

        # Query is already denied.
        status, _data = _req(f"{url}/v1/query?channel=dm:alice:bob", "GET", tok["eve"])
        assert status == 403

        # Claim used to return the payload even though query was denied.
        status, data = _req(
            f"{url}/v1/queue/claim", "POST", tok["eve"], {"channel": "dm:alice:bob"}
        )
        assert status == 403, data
        assert "private" in data["message"]
        assert "secret-for-bob" not in json.dumps(data)

        status, data = _req(f"{url}/v1/queue/ack", "POST", tok["eve"], {"message_id": message_id})
        assert status == 200
        assert data == {"acked": False}
        assert "secret-for-bob" not in json.dumps(data)

        status, data = _req(f"{url}/v1/queue/status?message_id={message_id}", "GET", tok["eve"])
        assert status == 404, data
        assert data["message"] == "No queue status for this message"
        assert "secret-for-bob" not in json.dumps(data)

        # The failed attempts did not touch the claim state.
        status, data = _req(f"{url}/v1/queue/status?message_id={message_id}", "GET", tok["bob"])
        assert status == 200
        assert data["status"]["status"] == "unclaimed"

    def test_participants_claim_ack_and_read_state(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        message_id = _publish(url, tok["alice"], "alice", "dm:alice:bob", "for bob")

        status, data = _req(
            f"{url}/v1/queue/claim", "POST", tok["bob"], {"channel": "dm:alice:bob"}
        )
        assert status == 200, data
        assert data["claimed"] is True
        assert data["result"]["message"]["payload"] == "for bob"
        assert data["result"]["claimed_by"] == "bob"

        status, data = _req(f"{url}/v1/queue/status?message_id={message_id}", "GET", tok["alice"])
        assert status == 200, data
        assert data["status"]["status"] == "claimed"

        status, data = _req(f"{url}/v1/queue/ack", "POST", tok["bob"], {"message_id": message_id})
        assert status == 200, data
        assert data["acked"] is True

        status, data = _req(f"{url}/v1/queue/status?message_id={message_id}", "GET", tok["alice"])
        assert status == 200, data
        assert data["status"]["status"] == "completed"

    def test_only_the_lessee_can_ack(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        message_id = _publish(url, tok["alice"], "alice", "dm:alice:bob", "task")

        status, data = _req(
            f"{url}/v1/queue/claim", "POST", tok["alice"], {"channel": "dm:alice:bob"}
        )
        assert status == 200, data

        # bob is a participant and may read, but alice holds the lease.
        status, data = _req(f"{url}/v1/queue/ack", "POST", tok["bob"], {"message_id": message_id})
        assert status == 200
        assert data == {"acked": False}

        status, data = _req(f"{url}/v1/queue/ack", "POST", tok["alice"], {"message_id": message_id})
        assert status == 200
        assert data["acked"] is True


class TestPublicQueue:
    def test_any_token_can_claim(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        message_id = _publish(url, tok["alice"], "alice", "work", "a task")

        status, data = _req(f"{url}/v1/queue/claim", "POST", tok["eve"], {"channel": "work"})
        assert status == 200, data
        assert data["claimed"] is True
        assert data["result"]["message"]["id"] == message_id

        status, data = _req(f"{url}/v1/queue/status?message_id={message_id}", "GET", tok["eve"])
        assert status == 200, data

        status, data = _req(f"{url}/v1/queue/ack", "POST", tok["eve"], {"message_id": message_id})
        assert status == 200
        assert data["acked"] is True


class TestBroadcastClaimDoesNotCreate:
    def test_claim_on_unregistered_broadcast_is_denied(self, mansio_server) -> None:
        url, store, bus, _server = mansio_server
        tok = _tokens(store)

        status, data = _req(
            f"{url}/v1/queue/claim", "POST", tok["eve"], {"channel": "broadcast:news"}
        )
        assert status == 403, data
        # The authorization check must not make the caller the channel owner.
        assert bus.get_channel_meta("broadcast:news") is None


class TestSuperTokenAndNoAuth:
    def test_supertoken_keeps_bypass(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tok = _tokens(store)
        message_id = _publish(url, tok["alice"], "alice", "dm:alice:bob", "dm task")
        supertoken = store.create_token(user_id=None, label="super")["token"]

        status, data = _req(
            f"{url}/v1/queue/claim",
            "POST",
            supertoken,
            {"channel": "dm:alice:bob", "claimed_by": "ops"},
        )
        assert status == 200, data
        assert data["claimed"] is True

        status, data = _req(f"{url}/v1/queue/status?message_id={message_id}", "GET", supertoken)
        assert status == 200, data

    def test_no_auth_mode_keeps_bypass(self, server_url) -> None:
        status, data = _req(
            f"{server_url}/v1/publish",
            "POST",
            None,
            {
                "channel": "dm:alice:bob",
                "sender": "alice",
                "msg_type": "task",
                "payload": "anon task",
                "queue": True,
            },
        )
        assert status == 200, data

        status, data = _req(
            f"{server_url}/v1/queue/claim",
            "POST",
            None,
            {"channel": "dm:alice:bob", "claimed_by": "anyone"},
        )
        assert status == 200, data
        assert data["claimed"] is True


@pytest.mark.parametrize(
    "channel,expected",
    [
        ("dm:alice:bob", False),
        ("notebook:alice", False),
        ("work", True),
    ],
)
def test_claim_denial_follows_channel_structure(mansio_server, channel, expected) -> None:
    from mansio_core.authz import ChannelAuthorizer

    _url, _store, bus, _server = mansio_server
    authz = ChannelAuthorizer(bus)
    assert (authz.claim_denial("eve", channel) is None) is expected
