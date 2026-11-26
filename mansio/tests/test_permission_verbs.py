"""Verb-based permissions and role presets (issue #247, slice 1).

Stored ACL permissions may be role presets (``viewer``/``member``/``owner``),
single verbs (``read``/``publish``/``delete``/``manage``), or the legacy levels
(``read``/``write``/``admin``).  Legacy levels and legacy ``check_access``
arguments keep working through aliases.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse

import pytest

from mansio import (
    ALLOWED_PERMISSIONS,
    ROLE_PRESETS,
    VERBS,
    ACLEntry,
    Bus,
    ChannelMeta,
    MemoryBackend,
    SQLiteBackend,
    permission_allows,
)
from mansio.frontends.http import HttpFrontend
from mansio.types import expand_permission, normalize_required


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class TestVocabulary:
    """The verb, preset, and alias tables."""

    def test_verbs(self) -> None:
        assert frozenset({"read", "publish", "delete", "manage"}) == VERBS

    def test_presets(self) -> None:
        assert ROLE_PRESETS["viewer"] == frozenset({"read"})
        assert ROLE_PRESETS["member"] == frozenset({"read", "publish"})
        assert ROLE_PRESETS["owner"] == frozenset({"read", "publish", "delete", "manage"})

    @pytest.mark.parametrize(
        ("permission", "expected"),
        [
            ("viewer", {"read"}),
            ("member", {"read", "publish"}),
            ("owner", {"read", "publish", "delete", "manage"}),
            ("read", {"read"}),
            ("publish", {"publish"}),
            ("delete", {"delete"}),
            ("manage", {"manage"}),
            ("write", {"read", "publish"}),
            ("admin", {"read", "publish", "delete", "manage"}),
        ],
    )
    def test_expand_permission(self, permission: str, expected: set[str]) -> None:
        assert expand_permission(permission) == frozenset(expected)

    def test_unknown_permission_grants_nothing(self) -> None:
        assert expand_permission("superadmin") == frozenset()

    def test_legacy_required_names_normalize(self) -> None:
        assert normalize_required("read") == "read"
        assert normalize_required("write") == "publish"
        assert normalize_required("admin") == "manage"
        assert normalize_required("delete") == "delete"

    def test_allowed_permissions_cover_presets_verbs_and_legacy(self) -> None:
        assert {"viewer", "member", "owner"} <= ALLOWED_PERMISSIONS
        assert {"read", "publish", "delete", "manage"} <= ALLOWED_PERMISSIONS
        assert {"write", "admin"} <= ALLOWED_PERMISSIONS


class TestPermissionAllows:
    def test_member(self) -> None:
        assert permission_allows("member", "read")
        assert permission_allows("member", "publish")
        assert not permission_allows("member", "delete")
        assert not permission_allows("member", "manage")

    def test_owner(self) -> None:
        for verb in VERBS:
            assert permission_allows("owner", verb)

    def test_single_verb_does_not_imply_read(self) -> None:
        assert permission_allows("publish", "publish")
        assert not permission_allows("publish", "read")

    def test_legacy_required_arguments(self) -> None:
        assert permission_allows("member", "write")
        assert permission_allows("owner", "admin")
        assert not permission_allows("member", "admin")
        assert not permission_allows("viewer", "write")


@pytest.mark.parametrize(
    "make_backend",
    [MemoryBackend, lambda: SQLiteBackend(":memory:")],
    ids=["memory", "sqlite"],
)
class TestBackendVerbs:
    def _backend(self, make_backend):
        backend = make_backend()
        backend.create_channel(
            ChannelMeta(name="priv", owner="alice", visibility="private", created_at="t"),
            [
                ACLEntry(channel="priv", user_id="viewer", permission="viewer", granted_at="t"),
                ACLEntry(channel="priv", user_id="member", permission="member", granted_at="t"),
                ACLEntry(channel="priv", user_id="owner", permission="owner", granted_at="t"),
                ACLEntry(channel="priv", user_id="publisher", permission="publish", granted_at="t"),
            ],
        )
        return backend

    def test_preset_viewer(self, make_backend) -> None:
        backend = self._backend(make_backend)
        assert backend.check_access("priv", "viewer", "read")
        assert not backend.check_access("priv", "viewer", "publish")

    def test_preset_member(self, make_backend) -> None:
        backend = self._backend(make_backend)
        assert backend.check_access("priv", "member", "read")
        assert backend.check_access("priv", "member", "publish")
        assert not backend.check_access("priv", "member", "delete")
        assert not backend.check_access("priv", "member", "manage")

    def test_preset_owner(self, make_backend) -> None:
        backend = self._backend(make_backend)
        for verb in VERBS:
            assert backend.check_access("priv", "owner", verb)

    def test_single_publish_verb(self, make_backend) -> None:
        backend = self._backend(make_backend)
        assert backend.check_access("priv", "publisher", "publish")
        assert not backend.check_access("priv", "publisher", "read")
        assert not backend.check_access("priv", "publisher", "delete")

    def test_legacy_entries_still_work(self, make_backend) -> None:
        backend = self._backend(make_backend)
        backend.add_acl_entry(
            ACLEntry(channel="priv", user_id="legacy", permission="write", granted_at="t")
        )
        assert backend.check_access("priv", "legacy", "read")
        assert backend.check_access("priv", "legacy", "publish")
        assert not backend.check_access("priv", "legacy", "delete")


class TestBusVerbs:
    def test_member_can_publish_but_not_delete(self) -> None:
        bus = Bus(MemoryBackend())
        bus.create_channel("ch", "alice", visibility="private")
        bus.add_acl_entry(
            ACLEntry(channel="ch", user_id="bob", permission="member", granted_at="t")
        )

        assert bus.publish("ch", "bob", "text", "hi", enforce_acl=True)
        with pytest.raises(PermissionError, match="delete permission"):
            bus.delete_channel("ch", user_id="bob")
        bus.close()

    def test_owner_preset_can_delete(self) -> None:
        bus = Bus(MemoryBackend())
        bus.create_channel("ch", "alice", visibility="private")
        bus.publish("ch", "bob", "text", "hi")
        bus.add_acl_entry(ACLEntry(channel="ch", user_id="bob", permission="owner", granted_at="t"))

        assert bus.delete_channel("ch", user_id="bob") == 1
        bus.close()


class TestAclApiPermissions:
    @pytest.fixture()
    def http_client(self):
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

    def _req(self, method: str, url: str, body: dict | None = None):
        parsed = urllib.parse.urlparse(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        raw = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if raw else {}
        conn.request(method, parsed.path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        status = resp.status
        conn.close()
        return status, data

    def test_add_accepts_preset_and_verb(self, http_client) -> None:
        base, bus = http_client
        bus.create_channel("ch", "alice")
        for permission in ("owner", "publish", "manage", "write"):
            status, data = self._req(
                "POST",
                f"{base}/v1/channels/ch/acl",
                {"user_id": f"user-{permission}", "permission": permission},
            )
            assert status == 201, data
            assert data["entry"]["permission"] == permission

    def test_add_rejects_unknown(self, http_client) -> None:
        base, bus = http_client
        bus.create_channel("ch", "alice")
        status, data = self._req(
            "POST",
            f"{base}/v1/channels/ch/acl",
            {"user_id": "bob", "permission": "superadmin"},
        )
        assert status == 400
        assert "permission must be one of" in data["message"]

    def test_set_rejects_unknown(self, http_client) -> None:
        base, bus = http_client
        bus.create_channel("ch", "alice")
        status, data = self._req(
            "PUT",
            f"{base}/v1/channels/ch/acl",
            {"acl": [{"user_id": "bob", "permission": "superadmin"}]},
        )
        assert status == 400
        assert "permission must be one of" in data["message"]

    @pytest.mark.parametrize("permission", [{}, [], 123, None, True])
    def test_add_rejects_non_string(self, http_client, permission) -> None:
        """Unhashable or non-string values are a 400, not a 500."""
        base, bus = http_client
        bus.create_channel("ch", "alice")
        status, data = self._req(
            "POST",
            f"{base}/v1/channels/ch/acl",
            {"user_id": "bob", "permission": permission},
        )
        assert status == 400, data
        assert "permission must be one of" in data["message"]

    @pytest.mark.parametrize("permission", [{}, [], 123, None, True])
    def test_set_rejects_non_string(self, http_client, permission) -> None:
        base, bus = http_client
        bus.create_channel("ch", "alice")
        status, data = self._req(
            "PUT",
            f"{base}/v1/channels/ch/acl",
            {"acl": [{"user_id": "bob", "permission": permission}]},
        )
        assert status == 400, data
        assert "permission must be one of" in data["message"]
