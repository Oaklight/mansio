"""Broadcast channel write access follows the stored owner and ACL (issue #298).

The HTTP frontend used to reject every user-bound publish to ``broadcast:*``
before consulting the channel owner or its ACL, so ``PUT/POST
/v1/channels/<channel>/acl`` succeeded and then had no effect.  These tests
pin the intended rule: the channel owner (first publisher) and agents with an
explicit write/admin entry may post; everyone else is refused; reads stay
public.
"""

from __future__ import annotations

import threading

import pytest

from mansio import Bus, MemoryBackend
from mansio._vendor.httpclient import Client as HttpClient
from mansio.frontends.http import _broadcast_write_allowed

CHANNEL = "broadcast:news"
RACE_CHANNEL = "broadcast:race"


def _post_publish(
    url: str, token: str, sender: str, payload: str = "hello", channel: str = CHANNEL
):
    http = HttpClient(headers={"Authorization": f"Bearer {token}"})
    try:
        return http.post(
            f"{url}/v1/publish",
            json={
                "channel": channel,
                "sender": sender,
                "msg_type": "announcement",
                "payload": payload,
            },
        )
    finally:
        http.close()


def _post_acl(url: str, token: str, user_id: str, permission: str):
    http = HttpClient(headers={"Authorization": f"Bearer {token}"})
    try:
        return http.post(
            f"{url}/v1/channels/{CHANNEL}/acl",
            json={"user_id": user_id, "permission": permission},
        )
    finally:
        http.close()


@pytest.fixture()
def broadcast_client(mansio_server):
    """Create one token per agent; return (url, tokens)."""
    url, token_store, bus, server = mansio_server

    def make(user_id: str) -> str:
        return token_store.create_token(user_id=user_id, label=f"{user_id} token")["token"]

    return url, {
        "owner": make("owner"),
        "writer": make("writer"),
        "reader": make("reader"),
        "stranger": make("stranger"),
    }


class TestBroadcastOwner:
    def test_first_publisher_creates_and_can_post_again(self, broadcast_client) -> None:
        url, tokens = broadcast_client
        first = _post_publish(url, tokens["owner"], "owner", "one")
        assert first.status_code == 200, first.json()
        second = _post_publish(url, tokens["owner"], "owner", "two")
        assert second.status_code == 200, second.json()

    def test_non_owner_without_grant_is_refused(self, broadcast_client) -> None:
        url, tokens = broadcast_client
        assert _post_publish(url, tokens["owner"], "owner").status_code == 200
        denied = _post_publish(url, tokens["stranger"], "stranger")
        assert denied.status_code == 403
        assert "granted write access" in denied.json()["message"]


class TestBroadcastAclGrant:
    def test_write_grant_allows_publish(self, broadcast_client) -> None:
        url, tokens = broadcast_client
        assert _post_publish(url, tokens["owner"], "owner").status_code == 200

        granted = _post_acl(url, tokens["owner"], "writer", "write")
        assert granted.status_code == 201, granted.json()

        allowed = _post_publish(url, tokens["writer"], "writer")
        assert allowed.status_code == 200, allowed.json()

    def test_read_grant_does_not_allow_publish(self, broadcast_client) -> None:
        url, tokens = broadcast_client
        assert _post_publish(url, tokens["owner"], "owner").status_code == 200
        assert _post_acl(url, tokens["owner"], "reader", "read").status_code == 201

        denied = _post_publish(url, tokens["reader"], "reader")
        assert denied.status_code == 403

    def test_non_owner_cannot_grant_access(self, broadcast_client) -> None:
        url, tokens = broadcast_client
        assert _post_publish(url, tokens["owner"], "owner").status_code == 200

        denied = _post_acl(url, tokens["stranger"], "writer", "write")
        assert denied.status_code == 403


class TestBroadcastReadStaysPublic:
    def test_any_agent_can_read_a_broadcast_channel(self, broadcast_client) -> None:
        url, tokens = broadcast_client
        assert _post_publish(url, tokens["owner"], "owner", "release note").status_code == 200

        http = HttpClient(headers={"Authorization": f"Bearer {tokens['reader']}"})
        try:
            resp = http.get(f"{url}/v1/query?channel={CHANNEL}")
        finally:
            http.close()

        assert resp.status_code == 200, resp.json()
        payloads = [m["payload"] for m in resp.json()["messages"]]
        assert "release note" in payloads


class TestFirstPublisherRace:
    """#333: concurrent first publishers must not both be authorized."""

    def test_concurrent_first_publishers_only_one_wins(self) -> None:
        bus = Bus(backend=MemoryBackend())
        results: dict[str, bool] = {}
        barrier = threading.Barrier(2)

        def attempt(user_id: str) -> None:
            barrier.wait()
            results[user_id] = _broadcast_write_allowed(bus, RACE_CHANNEL, user_id)

        threads = [
            threading.Thread(target=attempt, args=(user,)) for user in ("agent-a", "agent-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(results.values()) == [False, True]
        winner = next(user for user, allowed in results.items() if allowed)
        meta = bus.get_channel_meta(RACE_CHANNEL)
        assert meta is not None
        assert meta.owner == winner

    def test_loser_cannot_publish_over_http(self, mansio_server) -> None:
        url, token_store, bus, server = mansio_server
        winner, loser = "agent-a", "agent-b"
        bus.ensure_channel(RACE_CHANNEL, owner=winner)
        tokens = {user: token_store.create_token(user, "race")["token"] for user in (winner, loser)}

        assert _post_publish(url, tokens[loser], loser, channel=RACE_CHANNEL).status_code == 403
        assert _post_publish(url, tokens[winner], winner, channel=RACE_CHANNEL).status_code == 200
