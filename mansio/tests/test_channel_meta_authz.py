"""Authorization for ``GET /v1/channels/<channel>/meta`` (issue #353).

The channel metadata route used to return owner/visibility/created_at to any
authenticated scoped token.  It now applies the same read rule as the channel
list and query paths: public channels for everyone, dm participants and
notebook/memory owners for private ones.  A denial is a 404 with the
not-found message, so the response does not confirm that a private channel
exists.  Supertokens and no-auth mode keep bypassing the check.
"""

from __future__ import annotations

import http.client
import json
import urllib.parse

import pytest


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


def _create(url: str, token: str, name: str, visibility: str = "private") -> None:
    status, data = _req(
        f"{url}/v1/channels",
        "POST",
        token,
        {"name": name, "visibility": visibility},
    )
    assert status == 201, data


class TestChannelMetaAuthz:
    def test_member_reads_dm_meta(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tokens = _tokens(store)
        _create(url, tokens["alice"], "dm:alice:bob")

        for user in ("alice", "bob"):
            status, data = _req(f"{url}/v1/channels/dm:alice:bob/meta", token=tokens[user])
            assert status == 200, data
            assert data["channel"]["name"] == "dm:alice:bob"
            assert data["channel"]["owner"] == "alice"

    @pytest.mark.parametrize(
        "channel",
        ["dm:alice:bob", "notebook:alice", "memory:alice"],
    )
    def test_stranger_gets_404_on_existing_private_channel(
        self, mansio_server, channel: str
    ) -> None:
        url, store, bus, _server = mansio_server
        tokens = _tokens(store)
        _create(url, tokens["alice"], channel)
        # The meta row exists, so the 404 is an authorization denial and not
        # a missing channel.
        assert bus.get_channel_meta(channel) is not None

        status, data = _req(f"{url}/v1/channels/{channel}/meta", token=tokens["eve"])
        assert status == 404, data
        assert data["message"] == f"Channel '{channel}' not found"

        unknown_status, unknown_data = _req(
            f"{url}/v1/channels/no-such-channel/meta", token=tokens["eve"]
        )
        assert unknown_status == 404
        assert unknown_data["message"] == "Channel 'no-such-channel' not found"

    def test_public_channel_meta_open_to_any_token(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tokens = _tokens(store)
        _create(url, tokens["alice"], "team", visibility="public")

        status, data = _req(f"{url}/v1/channels/team/meta", token=tokens["eve"])
        assert status == 200, data
        assert data["channel"]["visibility"] == "public"

    def test_supertoken_bypasses(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tokens = _tokens(store)
        _create(url, tokens["alice"], "dm:alice:bob")
        supertoken = store.create_token(user_id=None, label="super")["token"]

        status, data = _req(f"{url}/v1/channels/dm:alice:bob/meta", token=supertoken)
        assert status == 200, data

    def test_no_auth_mode_bypasses(self, server_url) -> None:
        status, data = _req(
            f"{server_url}/v1/channels",
            "POST",
            None,
            {"name": "notebook:alice", "owner": "alice", "visibility": "private"},
        )
        assert status == 201, data

        status, data = _req(f"{server_url}/v1/channels/notebook:alice/meta")
        assert status == 200, data

    def test_acl_read_still_requires_manage(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        tokens = _tokens(store)
        _create(url, tokens["alice"], "team", visibility="public")

        status, data = _req(f"{url}/v1/channels/team/acl", token=tokens["bob"])
        assert status == 403, data
        assert "lacks manage permission" in data["message"]

        status, data = _req(f"{url}/v1/channels/team/acl", token=tokens["alice"])
        assert status == 200, data
