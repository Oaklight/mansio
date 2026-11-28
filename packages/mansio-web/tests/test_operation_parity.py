"""Wire-level parity guard for the M6c operations migration.

Each test observes HTTP only.  The behaviour pinned here — the identity the
server derives from the token, the visibility and manage gates, and the
denial bodies that avoid confirming a private object exists — is what the
M6c move into ``operations.channels/acl/queue/presence`` must preserve.
No assertion depends on routing internals.
"""

from __future__ import annotations

import http.client
import json
import urllib.parse

QUEUE_CHANNEL = "queue-lane"


def _req(
    url: str,
    method: str = "GET",
    token: str | None = None,
    body: dict | None = None,
) -> tuple[int, dict]:
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
        user: store.create_token(user_id=user, label=f"{user} parity token")["token"]
        for user in ("alice", "bob", "carol", "eve")
    }


def _supertoken(store) -> str:
    return store.create_token(user_id=None, label="parity supertoken")["token"]


def _create(url: str, token: str, name: str, **extra) -> tuple[int, dict]:
    return _req(f"{url}/v1/channels", "POST", token, {"name": name, **extra})


def _publish(url: str, token: str, sender: str, channel: str, **extra) -> str:
    status, data = _req(
        f"{url}/v1/publish",
        "POST",
        token,
        {"channel": channel, "sender": sender, "msg_type": "chat", "payload": "parity", **extra},
    )
    assert status == 200, data
    return data["message_id"]


def _add_acl(url: str, token: str, channel: str, user_id: str, permission: str) -> None:
    status, data = _req(
        f"{url}/v1/channels/{channel}/acl",
        "POST",
        token,
        {"user_id": user_id, "permission": permission},
    )
    assert status == 201, data


def _acl_entries(url: str, token: str, channel: str) -> dict[str, dict]:
    status, data = _req(f"{url}/v1/channels/{channel}/acl", "GET", token)
    assert status == 200, data
    return {entry["user_id"]: entry for entry in data["acl"]}


# ── Channels ────────────────────────────────────────────────────────────


def test_scoped_create_forces_the_token_user_as_owner(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)

    status, data = _create(url, tok["alice"], "team", owner="bob")

    assert status == 201, data
    owner = data["channel"]["owner"]
    assert owner == "alice"
    assert owner not in ("", "admin")


def test_supertoken_create_requires_and_records_an_explicit_owner(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    supertoken = _supertoken(store)

    status, data = _create(url, supertoken, "team")
    assert status == 400, data
    assert data == {
        "error": "Bad Request",
        "message": "'owner' is required for supertoken/no-auth",
    }

    status, data = _create(url, supertoken, "team", owner="operator")
    assert status == 201, data
    assert data["channel"]["owner"] == "operator"


def test_private_listing_uses_the_read_rule_in_both_modes(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)
    supertoken = _supertoken(store)
    assert _create(url, tok["alice"], "ops", visibility="private")[0] == 201
    _publish(url, tok["alice"], "alice", "ops")
    _add_acl(url, tok["alice"], "ops", "bob", "viewer")

    def visible(token: str, detail: bool) -> set[str]:
        suffix = "?detail=true" if detail else ""
        status, data = _req(f"{url}/v1/channels{suffix}", "GET", token)
        assert status == 200, data
        return {ch["name"] if isinstance(ch, dict) else ch for ch in data["channels"]}

    for detail in (False, True):
        assert "ops" not in visible(tok["eve"], detail), detail
        assert "ops" in visible(tok["bob"], detail), detail
        assert "ops" in visible(supertoken, detail), detail


def test_channel_meta_hides_a_private_channel_from_an_outsider(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)
    assert _create(url, tok["alice"], "ops", visibility="private")[0] == 201
    _add_acl(url, tok["alice"], "ops", "bob", "viewer")

    status, denied = _req(f"{url}/v1/channels/ops/meta", "GET", tok["eve"])
    assert status == 404, denied
    assert denied == {"error": "Not Found", "message": "Channel 'ops' not found"}

    unknown_status, unknown = _req(f"{url}/v1/channels/missing-lane/meta", "GET", tok["eve"])
    assert unknown_status == 404
    assert unknown == {"error": "Not Found", "message": "Channel 'missing-lane' not found"}

    status, data = _req(f"{url}/v1/channels/ops/meta", "GET", tok["bob"])
    assert status == 200, data
    assert data["channel"]["name"] == "ops"


def test_delete_channel_is_owner_only(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)
    assert _create(url, tok["alice"], "ops")[0] == 201
    _publish(url, tok["alice"], "alice", "ops")

    status, data = _req(f"{url}/v1/channels/ops", "DELETE", tok["bob"])
    assert status == 403, data
    assert data == {
        "error": "Forbidden",
        "message": "Scoped tokens cannot delete shared channels",
    }

    status, data = _req(f"{url}/v1/channels/ops", "DELETE", tok["alice"])
    assert status == 200, data
    assert data == {"deleted": 1, "channel": "ops"}


def test_delete_missing_channel_keeps_the_caller_specific_status(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)
    supertoken = _supertoken(store)

    status, data = _req(f"{url}/v1/channels/ghost-lane", "DELETE", tok["bob"])
    assert status == 403, data
    assert data == {
        "error": "Forbidden",
        "message": "Scoped tokens cannot delete shared channels",
    }

    status, data = _req(f"{url}/v1/channels/ghost-lane", "DELETE", supertoken)
    assert status == 404, data
    assert data == {
        "error": "Not Found",
        "message": "Channel 'ghost-lane' not found or already empty",
    }


# ── ACL ─────────────────────────────────────────────────────────────────


def test_scoped_acl_writes_record_the_caller_as_grantor(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)
    assert _create(url, tok["alice"], "ops")[0] == 201

    _add_acl(url, tok["alice"], "ops", "bob", "viewer")
    assert _acl_entries(url, tok["alice"], "ops")["bob"]["granted_by"] == "alice"

    status, data = _req(
        f"{url}/v1/channels/ops/acl",
        "PUT",
        tok["alice"],
        {"acl": [{"user_id": "carol", "permission": "member"}]},
    )
    assert status == 200, data
    assert data == {"status": "ok", "count": 1}
    assert _acl_entries(url, tok["alice"], "ops")["carol"]["granted_by"] == "alice"


def test_supertoken_acl_writes_record_no_grantor(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    supertoken = _supertoken(store)
    assert _create(url, supertoken, "ops", owner="operator")[0] == 201

    _add_acl(url, supertoken, "ops", "bob", "viewer")
    assert _acl_entries(url, supertoken, "ops")["bob"]["granted_by"] is None

    status, data = _req(
        f"{url}/v1/channels/ops/acl",
        "PUT",
        supertoken,
        {"acl": [{"user_id": "carol", "permission": "member"}]},
    )
    assert status == 200, data
    assert _acl_entries(url, supertoken, "ops")["carol"]["granted_by"] is None


def test_acl_access_uses_the_manage_gate(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)
    assert _create(url, tok["alice"], "ops", visibility="private")[0] == 201
    _add_acl(url, tok["alice"], "ops", "bob", "viewer")

    add = {"user_id": "carol", "permission": "member"}
    status, data = _req(f"{url}/v1/channels/ops/acl", "POST", tok["bob"], add)
    assert status == 403, data
    assert data == {
        "error": "Forbidden",
        "message": "Agent 'bob' lacks manage permission on 'ops'",
    }

    status, data = _req(f"{url}/v1/channels/ops/acl", "GET", tok["bob"])
    assert status == 403, data
    assert data == {
        "error": "Forbidden",
        "message": "Agent 'bob' lacks manage permission on 'ops'",
    }

    status, data = _req(f"{url}/v1/channels/ops/acl", "GET", tok["eve"])
    assert status == 403, data
    assert data == {
        "error": "Forbidden",
        "message": "Agent 'eve' lacks manage permission on 'ops'",
    }

    assert _acl_entries(url, tok["alice"], "ops")["bob"]["permission"] == "viewer"


# ── Queue ───────────────────────────────────────────────────────────────


def test_scoped_claim_ignores_the_body_claimed_by(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)
    _publish(url, tok["alice"], "alice", QUEUE_CHANNEL, queue=True)

    status, data = _req(
        f"{url}/v1/queue/claim",
        "POST",
        tok["alice"],
        {"channel": QUEUE_CHANNEL, "claimed_by": "bob"},
    )

    assert status == 200, data
    assert data["claimed"] is True
    assert data["result"]["claimed_by"] == "alice"


def test_supertoken_claim_requires_and_records_the_explicit_claimer(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)
    supertoken = _supertoken(store)
    _publish(url, tok["alice"], "alice", QUEUE_CHANNEL, queue=True)

    status, data = _req(
        f"{url}/v1/queue/claim",
        "POST",
        supertoken,
        {"channel": QUEUE_CHANNEL},
    )
    assert status == 400, data
    assert data == {"error": "Bad Request", "message": "Missing 'claimed_by'"}

    status, data = _req(
        f"{url}/v1/queue/claim",
        "POST",
        supertoken,
        {"channel": QUEUE_CHANNEL, "claimed_by": "dispatcher"},
    )
    assert status == 200, data
    assert data["claimed"] is True
    assert data["result"]["claimed_by"] == "dispatcher"


def test_queue_status_denial_is_the_no_state_body(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)
    message_id = _publish(url, tok["alice"], "alice", "dm:alice:bob", queue=True)

    # The state exists and is visible to a participant.
    status, data = _req(f"{url}/v1/queue/status?message_id={message_id}", "GET", tok["alice"])
    assert status == 200, data
    assert data["found"] is True

    status, denied = _req(f"{url}/v1/queue/status?message_id={message_id}", "GET", tok["eve"])
    assert status == 404, denied

    status, absent = _req(f"{url}/v1/queue/status?message_id=no-such-message", "GET", tok["eve"])
    assert status == 404, absent

    assert denied == absent
    assert denied == {"error": "Not Found", "message": "No queue status for this message"}


# ── Presence ────────────────────────────────────────────────────────────


def test_heartbeat_for_self_succeeds(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)

    status, data = _req(f"{url}/v1/presence/heartbeat", "POST", tok["alice"], {"user_id": "alice"})

    assert status == 200, data
    assert data == {"ok": True}


def test_scoped_heartbeat_as_another_user_is_forbidden(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    tok = _tokens(store)

    status, data = _req(f"{url}/v1/presence/heartbeat", "POST", tok["alice"], {"user_id": "bob"})

    assert status == 403, data
    assert data == {
        "error": "Forbidden",
        "message": "Token bound to 'alice', cannot heartbeat as 'bob'",
    }


def test_supertoken_may_heartbeat_as_anyone(mansio_server) -> None:
    url, store, _bus, _server = mansio_server
    supertoken = _supertoken(store)

    status, data = _req(f"{url}/v1/presence/heartbeat", "POST", supertoken, {"user_id": "agent-z"})

    assert status == 200, data
    assert data == {"ok": True}
