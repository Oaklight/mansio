"""Reject malformed ``dm:`` channel names (issues #345, #352).

A DM channel name is exactly ``dm:<user_a>:<user_b>``, and each slot must be a
valid agent id: 3-64 characters, lowercase alphanumerics and hyphens, starting
and ending with an alphanumeric.  Before #345, ``dm:a`` was accepted at
publish time but no user could ever be a member of it; before #352, the same
held for three-segment names whose slots were not valid agent ids
(``dm:a:b``, ``dm:Alice:bob``, ``dm:alice.bob:carol``).

The canonical parser in ``mansio_core.authz`` now gates the publish, channel
create, query, and subscribe paths, and uses the shared user-id rule from
``mansio_core/types.py`` for both participant slots.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse

import pytest
from conftest import make_client
from mansio_client.transport import MansioAPIError
from mansio_core import Bus, MemoryBackend
from mansio_core.authz import (
    ChannelAuthorizer,
    dm_channel_error,
    dm_participants,
    is_channel_member,
    is_private_channel,
)
from mansio_core.types import is_valid_user_id
from mansio_web.frontends.http import HttpFrontend

MALFORMED_DM_NAMES = [
    "dm:a",
    "dm:a:b:c",
    "dm::",
    "dm:",
    "dm::b",
    "dm:a:",
    "dm:a:b:",
]

# Structurally three segments, but a segment contains a character that
# cannot appear in a channel name (and so cannot name a user).
UNUSABLE_DM_NAMES = [
    "dm:a b:c",
    "dm:alice:bob-",
    "dm:alice:bob.",
]

# Structurally three segments, but a slot is not a valid agent id, so no
# token can ever act as that participant (#352).
INVALID_USER_ID_DM_NAMES = [
    "dm:a:b",  # 1-char ids
    "dm:ab:cd",  # 2-char ids
    "dm:Alice:bob",  # uppercase
    "dm:alice.bob:carol",  # dot
    "dm:alice_bob:carol",  # underscore
    "dm:-alice:bob",  # leading hyphen
    "dm:alice-:bob",  # trailing hyphen
]

ALL_REJECTED_DM_NAMES = MALFORMED_DM_NAMES + UNUSABLE_DM_NAMES + INVALID_USER_ID_DM_NAMES


class TestDmParser:
    def test_well_formed(self) -> None:
        assert dm_participants("dm:alice:bob") == ("alice", "bob")
        assert dm_participants("dm:agent-1:agent-2") == ("agent-1", "agent-2")
        assert dm_channel_error("dm:alice:bob") is None

    @pytest.mark.parametrize("channel", MALFORMED_DM_NAMES + INVALID_USER_ID_DM_NAMES)
    def test_malformed(self, channel: str) -> None:
        assert dm_participants(channel) is None
        assert dm_channel_error(channel) is not None
        # Fail closed: an existing malformed row stays private and has no
        # members, so only its sender is considered involved.
        assert is_private_channel(channel)

    def test_slots_follow_the_shared_user_id_rule(self) -> None:
        for first, second in (
            ("alice", "bob"),
            ("a", "b"),
            ("ab", "cd"),
            ("Alice", "bob"),
            ("alice.bob", "carol"),
            ("alice-", "bob"),
        ):
            expected = is_valid_user_id(first) and is_valid_user_id(second)
            assert (dm_participants(f"dm:{first}:{second}") is not None) is expected

    def test_short_ids_are_now_rejected(self) -> None:
        """#352 behavior change: ``dm:a:b`` used to parse; no token can use
        those ids, so the channel was writable by nobody."""
        assert dm_participants("dm:a:b") is None
        assert dm_channel_error("dm:a:b") is not None
        assert not is_channel_member("a", "dm:a:b")

    def test_trailing_newline_in_id_is_rejected(self) -> None:
        """#384: ``$`` matched before a trailing newline, so ``alice\\n``
        passed the user-id rule and parsed as a DM participant."""
        assert not is_valid_user_id("alice\n")
        assert dm_participants("dm:alice\n:bob") is None
        assert dm_channel_error("dm:alice:bob\n") is not None

    @pytest.mark.parametrize(
        "channel", ["general", "notebook:alice", "memory:bob", "broadcast:news"]
    )
    def test_non_dm_names_are_not_rejected(self, channel: str) -> None:
        assert dm_channel_error(channel) is None

    def test_extra_colon_is_not_a_participant(self) -> None:
        """``split(':', 2)`` used to read ``b:c`` as a participant."""
        assert not is_channel_member("b:c", "dm:a:b:c")
        assert not is_channel_member("a", "dm:a:b:c")

    def test_undefined_is_not_special_cased(self) -> None:
        """The literal user id ``undefined`` is not special; two segments
        are structurally valid, so both slots name that same user."""
        assert dm_participants("dm:undefined:undefined") == ("undefined", "undefined")
        assert dm_channel_error("dm:undefined:undefined") is None


class TestDmAuthorization:
    def test_participants_are_members_and_may_delete(self) -> None:
        bus = Bus(MemoryBackend())
        authz = ChannelAuthorizer(bus)
        try:
            assert authz.can_publish("alice", "dm:alice:bob")
            assert authz.can_publish("bob", "dm:alice:bob")
            assert authz.can_read("alice", "dm:alice:bob")
            assert authz.can_read("bob", "dm:alice:bob")
            assert authz.can_delete_channel("alice", "dm:alice:bob")
            assert authz.can_delete_channel("bob", "dm:alice:bob")

            assert not authz.can_publish("eve", "dm:alice:bob")
            assert not authz.can_read("eve", "dm:alice:bob")
            assert not authz.can_delete_channel("eve", "dm:alice:bob")
        finally:
            bus.close()

    @pytest.mark.parametrize("channel", ALL_REJECTED_DM_NAMES)
    def test_no_user_is_a_member_of_a_malformed_dm(self, channel: str) -> None:
        for user_id in ("alice", "bob", "a", "b", ""):
            assert not is_channel_member(user_id, channel)


@pytest.fixture()
def http_server():
    """Start an HTTP frontend without token auth, yield (base_url, bus)."""
    bus = Bus(backend=MemoryBackend())
    frontend = HttpFrontend(port=0, allow_unauthenticated=True)
    frontend.attach(bus)
    thread = threading.Thread(target=frontend.serve_forever, daemon=True)
    thread.start()
    for _ in range(50):
        if frontend._app.port is not None:
            break
        time.sleep(0.05)
    host, port = frontend.address
    yield f"http://{host}:{port}", bus
    frontend.shutdown()
    bus.close()


def _http_req(method: str, url: str, body: dict | None = None):
    parsed = urllib.parse.urlparse(url)
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    raw = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if raw else {}
    conn.request(method, target, body=raw, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    status = resp.status
    conn.close()
    return status, data


class TestHttpRejectsMalformedDm:
    @pytest.mark.parametrize("channel", ALL_REJECTED_DM_NAMES)
    def test_publish_is_a_client_error(self, http_server, channel: str) -> None:
        """Regression: publish used to accept the name and create a channel
        that no user could write to afterwards."""
        base, bus = http_server
        status, data = _http_req(
            "POST",
            f"{base}/v1/publish",
            {
                "channel": channel,
                "sender": "alice",
                "msg_type": "chat",
                "payload": "should not be stored",
            },
        )
        assert status == 400, data
        assert "Invalid dm channel name" in data["message"]
        assert bus.get_channel_meta(channel) is None

    @pytest.mark.parametrize("channel", ALL_REJECTED_DM_NAMES)
    def test_create_is_a_client_error(self, http_server, channel: str) -> None:
        base, bus = http_server
        status, data = _http_req(
            "POST",
            f"{base}/v1/channels",
            {"name": channel, "owner": "alice"},
        )
        assert status == 400, data
        assert "Invalid dm channel name" in data["message"]
        assert bus.get_channel_meta(channel) is None

    @pytest.mark.parametrize("channel", ALL_REJECTED_DM_NAMES)
    def test_query_is_a_client_error(self, http_server, channel: str) -> None:
        base, _ = http_server
        quoted = urllib.parse.quote(channel, safe="")
        status, data = _http_req("GET", f"{base}/v1/query?channel={quoted}")
        assert status == 400, data
        assert "Invalid dm channel name" in data["message"]

    @pytest.mark.parametrize("channel", ALL_REJECTED_DM_NAMES)
    def test_subscribe_is_a_client_error(self, http_server, channel: str) -> None:
        base, _ = http_server
        quoted = urllib.parse.quote(channel, safe="")
        status, data = _http_req("GET", f"{base}/v1/subscribe?channel={quoted}")
        assert status == 400, data
        assert "Invalid dm channel name" in data["message"]


class TestHttpAcceptsWellFormedDm:
    def test_create_accepts_well_formed_dm(self, http_server) -> None:
        base, bus = http_server
        status, data = _http_req(
            "POST",
            f"{base}/v1/channels",
            {"name": "dm:alice:bob", "owner": "alice", "visibility": "private"},
        )
        assert status == 201, data
        assert data["channel"]["name"] == "dm:alice:bob"
        assert data["channel"]["visibility"] == "private"
        assert bus.get_channel_meta("dm:alice:bob") is not None

    def test_create_accepts_hyphenated_ids(self, http_server) -> None:
        base, bus = http_server
        status, data = _http_req(
            "POST",
            f"{base}/v1/channels",
            {
                "name": "dm:agent-1:agent-2",
                "owner": "agent-1",
                "visibility": "private",
            },
        )
        assert status == 201, data
        assert bus.get_channel_meta("dm:agent-1:agent-2") is not None

    def test_both_participants_publish_and_stranger_is_denied(self, mansio_server) -> None:
        url, store, _bus, _server = mansio_server
        alice = make_client(url, store, "alice")
        bob = make_client(url, store, "bob")
        eve = make_client(url, store, "eve")

        try:
            alice.channel_send("dm:alice:bob", "hello bob")
            bob.channel_send("dm:alice:bob", "hello alice")

            with pytest.raises(MansioAPIError) as excinfo:
                eve.channel_send("dm:alice:bob", "hello?")
            assert excinfo.value.status_code == 403

            assert len(bob.channel_read("dm:alice:bob")) == 2
        finally:
            alice.close()
            bob.close()
            eve.close()
