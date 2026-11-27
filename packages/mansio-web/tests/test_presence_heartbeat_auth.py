"""Presence heartbeats must respect the caller's token scope.

Regression tests for issue #295: ``POST /v1/presence/heartbeat`` used the
body's ``user_id`` verbatim and never compared it with the authenticated
caller, so any scoped token could mark another user online and attach
arbitrary metadata to them.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def _heartbeat(url: str, token: str, user_id: str, metadata: dict | None = None):
    """POST a heartbeat and return (status_code, response_body).

    HTTPError is not raised to the caller: 4xx responses are the
    behaviour under test, so their status and body are returned.
    """
    body = {"user_id": user_id}
    if metadata is not None:
        body["metadata"] = metadata
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{url}/v1/presence/heartbeat",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class TestScopedTokenCannotHeartbeatAsAnotherUser:
    def test_own_identity_is_accepted(self, mansio_server):
        url, token_store, bus, _server = mansio_server
        token = token_store.create_token(user_id="agent-a")["token"]

        status, body = _heartbeat(url, token, "agent-a")

        assert status == 200, body
        assert body == {"ok": True}
        assert [a.user_id for a in bus.users()] == ["agent-a"]

    def test_another_identity_is_rejected(self, mansio_server):
        url, token_store, bus, _server = mansio_server
        token = token_store.create_token(user_id="agent-a")["token"]

        status, body = _heartbeat(url, token, "agent-b", metadata={"display_name": "forged"})

        assert status == 403, body
        assert "agent-a" in body["message"]
        assert "agent-b" in body["message"]
        # The forged heartbeat must not create presence for agent-b. (agent-a
        # is present because authenticated activity records its own user.)
        assert "agent-b" not in {a.user_id for a in bus.users()}

    def test_rejected_heartbeat_does_not_overwrite_existing_presence(self, mansio_server):
        url, token_store, bus, _server = mansio_server
        token = token_store.create_token(user_id="agent-a")["token"]
        bus.heartbeat("agent-b", {"display_name": "real"})

        status, _body = _heartbeat(url, token, "agent-b", metadata={"display_name": "forged"})

        assert status == 403
        agent_b = next(a for a in bus.users() if a.user_id == "agent-b")
        assert agent_b.metadata == {"display_name": "real"}


class TestPrivilegedCallersMayActForAnyUser:
    def test_supertoken_can_heartbeat_as_any_user(self, mansio_server):
        url, token_store, bus, _server = mansio_server
        supertoken = token_store.create_token(user_id=None, label="admin")["token"]

        status, body = _heartbeat(url, supertoken, "agent-z")

        assert status == 200, body
        assert [a.user_id for a in bus.users()] == ["agent-z"]


class TestNoAuthModeMayActForAnyUser:
    def test_open_server_can_heartbeat_as_any_user(self, server_url):
        status, body = _heartbeat(server_url, "", "agent-y")

        assert status == 200, body


def _post_heartbeat_body(url: str, payload: bytes | None):
    """POST raw bytes to the heartbeat route and return (status, body)."""
    req = urllib.request.Request(
        f"{url}/v1/presence/heartbeat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class TestHeartbeatValidationEnvelope:
    """Every heartbeat validation failure uses the uniform error envelope."""

    def test_missing_user_id(self, server_url):
        status, body = _post_heartbeat_body(server_url, b"{}")

        assert status == 400
        assert body == {"error": "Bad Request", "message": "user_id required"}

    def test_non_string_user_id(self, server_url):
        status, body = _post_heartbeat_body(server_url, b'{"user_id": 5}')

        assert status == 400
        assert body == {"error": "Bad Request", "message": "user_id required"}

    def test_missing_body(self, server_url):
        status, body = _post_heartbeat_body(server_url, b"")

        assert status == 400
        assert body == {"error": "Bad Request", "message": "JSON body required"}

    def test_invalid_metadata(self, server_url):
        status, body = _post_heartbeat_body(server_url, b'{"user_id": "agent-y", "metadata": []}')

        assert status == 400
        assert body == {
            "error": "Bad Request",
            "message": "metadata must be a JSON object or null",
        }
