"""Tests for channel ownership and ACL — issue #113."""

from __future__ import annotations

import json
import threading
import time

import pytest

from mansio import (
    ACLEntry,
    Bus,
    ChannelMeta,
    ChannelStore,
    MemoryBackend,
    SQLiteBackend,
)
from mansio.frontends.http import HttpFrontend

# ──────────────────────────────────────────────
# Helpers / Fixtures
# ──────────────────────────────────────────────


@pytest.fixture()
def bus():
    b = Bus(MemoryBackend())
    yield b
    b.close()


@pytest.fixture()
def sqlite_bus(tmp_path):
    b = Bus(SQLiteBackend(str(tmp_path / "test.db")))
    yield b
    b.close()


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────


class TestTypes:
    """ChannelMeta and ACLEntry dataclass basics."""

    def test_channel_meta_fields(self):
        m = ChannelMeta(name="ch", owner="alice", visibility="public", created_at="t")
        assert m.name == "ch"
        assert m.owner == "alice"
        assert m.visibility == "public"

    def test_acl_entry_defaults(self):
        e = ACLEntry(channel="ch", agent_id="bob")
        assert e.permission == "write"
        assert e.granted_at == ""
        assert e.granted_by is None

    def test_acl_entry_explicit(self):
        e = ACLEntry(
            channel="ch",
            agent_id="bob",
            permission="admin",
            granted_at="t",
            granted_by="alice",
        )
        assert e.permission == "admin"
        assert e.granted_by == "alice"


# ──────────────────────────────────────────────
# Backend ChannelStore implementation
# ──────────────────────────────────────────────


class TestMemoryChannelStore:
    """MemoryBackend implements ChannelStore."""

    def test_implements_protocol(self):
        assert isinstance(MemoryBackend(), ChannelStore)

    def test_create_and_get(self):
        b = MemoryBackend()
        meta = ChannelMeta(name="ch", owner="alice", visibility="private", created_at="t")
        b.create_channel(meta)
        assert b.get_channel("ch") == meta
        b.close()

    def test_create_duplicate_raises(self):
        b = MemoryBackend()
        meta = ChannelMeta(name="ch", owner="alice", visibility="public", created_at="t")
        b.create_channel(meta)
        with pytest.raises(ValueError, match="already exists"):
            b.create_channel(meta)
        b.close()

    def test_create_with_acl(self):
        b = MemoryBackend()
        meta = ChannelMeta(name="ch", owner="alice", visibility="private", created_at="t")
        acl = [ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at="t")]
        b.create_channel(meta, acl)
        entries = b.get_acl("ch")
        assert len(entries) == 1
        assert entries[0].agent_id == "bob"
        b.close()

    def test_list_channels_meta(self):
        b = MemoryBackend()
        b.create_channel(ChannelMeta(name="b-ch", owner="a", visibility="public", created_at="t"))
        b.create_channel(ChannelMeta(name="a-ch", owner="a", visibility="public", created_at="t"))
        result = b.list_channels_meta()
        assert [c.name for c in result] == ["a-ch", "b-ch"]
        b.close()

    def test_update_channel(self):
        b = MemoryBackend()
        b.create_channel(ChannelMeta(name="ch", owner="alice", visibility="public", created_at="t"))
        assert b.update_channel("ch", visibility="private")
        assert b.get_channel("ch").visibility == "private"
        assert not b.update_channel("nonexistent", visibility="private")
        b.close()

    def test_delete_channel_meta(self):
        b = MemoryBackend()
        b.create_channel(ChannelMeta(name="ch", owner="alice", visibility="public", created_at="t"))
        acl = [ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at="t")]
        b.set_acl("ch", acl)
        assert b.delete_channel_meta("ch")
        assert b.get_channel("ch") is None
        assert b.get_acl("ch") == []
        assert not b.delete_channel_meta("ch")  # already gone
        b.close()

    def test_acl_crud(self):
        b = MemoryBackend()
        b.create_channel(ChannelMeta(name="ch", owner="a", visibility="private", created_at="t"))
        # add
        b.add_acl_entry(ACLEntry(channel="ch", agent_id="bob", permission="read", granted_at="t"))
        b.add_acl_entry(
            ACLEntry(channel="ch", agent_id="carol", permission="write", granted_at="t")
        )
        assert len(b.get_acl("ch")) == 2
        # upsert
        b.add_acl_entry(ACLEntry(channel="ch", agent_id="bob", permission="admin", granted_at="t"))
        entries = b.get_acl("ch")
        bob_entry = [e for e in entries if e.agent_id == "bob"][0]
        assert bob_entry.permission == "admin"
        # remove
        assert b.remove_acl_entry("ch", "bob")
        assert not b.remove_acl_entry("ch", "bob")  # already gone
        assert len(b.get_acl("ch")) == 1
        # replace all
        b.set_acl(
            "ch", [ACLEntry(channel="ch", agent_id="dave", permission="read", granted_at="t")]
        )
        entries = b.get_acl("ch")
        assert len(entries) == 1
        assert entries[0].agent_id == "dave"
        b.close()


class TestSQLiteChannelStore:
    """SQLiteBackend implements ChannelStore."""

    def test_implements_protocol(self):
        assert isinstance(SQLiteBackend(), ChannelStore)

    def test_create_and_get(self, tmp_path):
        b = SQLiteBackend(str(tmp_path / "test.db"))
        meta = ChannelMeta(name="ch", owner="alice", visibility="private", created_at="t")
        b.create_channel(meta)
        result = b.get_channel("ch")
        assert result == meta
        b.close()

    def test_create_duplicate_raises(self, tmp_path):
        b = SQLiteBackend(str(tmp_path / "test.db"))
        meta = ChannelMeta(name="ch", owner="alice", visibility="public", created_at="t")
        b.create_channel(meta)
        with pytest.raises(ValueError, match="already exists"):
            b.create_channel(meta)
        b.close()

    def test_acl_roundtrip(self, tmp_path):
        b = SQLiteBackend(str(tmp_path / "test.db"))
        meta = ChannelMeta(name="ch", owner="alice", visibility="private", created_at="t")
        acl = [
            ACLEntry(
                channel="ch", agent_id="bob", permission="write", granted_at="t", granted_by="alice"
            ),
        ]
        b.create_channel(meta, acl)
        entries = b.get_acl("ch")
        assert len(entries) == 1
        assert entries[0].agent_id == "bob"
        assert entries[0].granted_by == "alice"
        b.close()

    def test_delete_cascades_acl(self, tmp_path):
        b = SQLiteBackend(str(tmp_path / "test.db"))
        meta = ChannelMeta(name="ch", owner="alice", visibility="private", created_at="t")
        acl = [ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at="t")]
        b.create_channel(meta, acl)
        b.delete_channel_meta("ch")
        assert b.get_acl("ch") == []
        b.close()

    def test_update_channel(self, tmp_path):
        b = SQLiteBackend(str(tmp_path / "test.db"))
        b.create_channel(ChannelMeta(name="ch", owner="alice", visibility="public", created_at="t"))
        assert b.update_channel("ch", visibility="private", owner="bob")
        ch = b.get_channel("ch")
        assert ch.visibility == "private"
        assert ch.owner == "bob"
        b.close()

    def test_persistence(self, tmp_path):
        """Channel metadata survives close/reopen."""
        db = str(tmp_path / "persist.db")
        b1 = SQLiteBackend(db)
        b1.create_channel(
            ChannelMeta(name="ch", owner="alice", visibility="private", created_at="t"),
            [ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at="t")],
        )
        b1.close()
        b2 = SQLiteBackend(db)
        assert b2.get_channel("ch") is not None
        assert len(b2.get_acl("ch")) == 1
        b2.close()


# ──────────────────────────────────────────────
# Access control logic
# ──────────────────────────────────────────────


class TestCheckAccess:
    """check_access permission hierarchy."""

    @pytest.fixture()
    def backend(self):
        b = MemoryBackend()
        b.create_channel(
            ChannelMeta(name="priv", owner="alice", visibility="private", created_at="t"),
            [
                ACLEntry(channel="priv", agent_id="reader", permission="read", granted_at="t"),
                ACLEntry(channel="priv", agent_id="writer", permission="write", granted_at="t"),
                ACLEntry(channel="priv", agent_id="admin", permission="admin", granted_at="t"),
            ],
        )
        b.create_channel(
            ChannelMeta(name="pub", owner="alice", visibility="public", created_at="t"),
        )
        yield b
        b.close()

    # Owner
    def test_owner_has_all_access(self, backend):
        for perm in ("read", "write", "admin"):
            assert backend.check_access("priv", "alice", perm)

    # Permission hierarchy
    def test_read_only(self, backend):
        assert backend.check_access("priv", "reader", "read")
        assert not backend.check_access("priv", "reader", "write")
        assert not backend.check_access("priv", "reader", "admin")

    def test_write_implies_read(self, backend):
        assert backend.check_access("priv", "writer", "read")
        assert backend.check_access("priv", "writer", "write")
        assert not backend.check_access("priv", "writer", "admin")

    def test_admin_implies_all(self, backend):
        for perm in ("read", "write", "admin"):
            assert backend.check_access("priv", "admin", perm)

    # No access
    def test_no_acl_private_denied(self, backend):
        assert not backend.check_access("priv", "stranger", "read")

    # Public channels
    def test_public_read_write_open(self, backend):
        assert backend.check_access("pub", "stranger", "read")
        assert backend.check_access("pub", "stranger", "write")

    def test_public_admin_denied(self, backend):
        assert not backend.check_access("pub", "stranger", "admin")

    def test_public_owner_admin(self, backend):
        assert backend.check_access("pub", "alice", "admin")

    def test_public_explicit_read_acl_keeps_write(self, backend):
        """An explicit read ACL entry must not downgrade public write access."""
        entry = ACLEntry(channel="pub", agent_id="stranger", permission="read", granted_at=_now())
        backend.add_acl_entry(entry)
        assert backend.check_access("pub", "stranger", "write")

    def test_private_explicit_read_blocks_write(self, backend):
        """On private channels, an explicit read ACL should NOT grant write."""
        entry = ACLEntry(channel="priv", agent_id="stranger", permission="read", granted_at=_now())
        backend.add_acl_entry(entry)
        assert backend.check_access("priv", "stranger", "read")
        assert not backend.check_access("priv", "stranger", "write")

    # Unregistered channels (no metadata)
    def test_unregistered_is_open(self, backend):
        assert backend.check_access("unknown", "anyone", "read")


# ──────────────────────────────────────────────
# Bus-level channel management
# ──────────────────────────────────────────────


class TestBusChannelManagement:
    """Bus.create_channel, ensure_channel, check_access."""

    def test_create_channel(self, bus):
        meta = bus.create_channel("test", "alice", visibility="private")
        assert meta.name == "test"
        assert meta.owner == "alice"
        assert meta.visibility == "private"
        assert bus.get_channel_meta("test") is not None

    def test_create_duplicate_raises(self, bus):
        bus.create_channel("test", "alice")
        with pytest.raises(ValueError):
            bus.create_channel("test", "bob")

    def test_ensure_channel_idempotent(self, bus):
        m1 = bus.ensure_channel("test", "alice", visibility="private")
        m2 = bus.ensure_channel("test", "bob", visibility="public")
        assert m1.owner == "alice"
        assert m2.owner == "alice"  # second call returns existing

    def test_check_access_no_channel_store(self):
        """Bus.check_access returns True for non-ChannelStore backends."""
        # Create a minimal backend that is NOT ChannelStore
        from mansio.protocols import Backend

        class MinimalBackend(Backend):
            def store(self, msg):
                pass

            def store_queue(self, msg):
                pass

            def query(self, *a, **kw):
                return []

            def list_channels(self):
                return []

            def queue_claim(self, *a, **kw):
                return None

            def queue_ack(self, *a, **kw):
                return None

            def queue_status(self, mid):
                return None

        bus = Bus(MinimalBackend())
        assert bus.check_access("any", "any", "admin")
        bus.close()

    def test_bus_check_access(self, bus):
        bus.create_channel("priv", "alice", visibility="private")
        assert bus.check_access("priv", "alice", "admin")
        assert not bus.check_access("priv", "stranger", "read")

    def test_acl_management(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry = ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at=_now())
        bus.add_acl_entry(entry)
        assert bus.check_access("ch", "bob", "write")
        entries = bus.get_acl("ch")
        assert len(entries) == 1
        bus.remove_acl_entry("ch", "bob")
        assert not bus.check_access("ch", "bob", "read")

    def test_delete_channel_cleans_metadata(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        bus.publish("ch", "alice", "text", "hello")
        bus.delete_channel("ch")
        assert bus.get_channel_meta("ch") is None


# ──────────────────────────────────────────────
# Sugar auto-creation
# ──────────────────────────────────────────────


class TestSugarAutoCreation:
    """Bus.publish auto-creates channel metadata for sugar prefixes."""

    def test_dm_auto_create(self, bus):
        bus.publish("dm:alice:bob", "alice", "text", "hi")
        meta = bus.get_channel_meta("dm:alice:bob")
        assert meta is not None
        assert meta.visibility == "private"
        acl = bus.get_acl("dm:alice:bob")
        agent_ids = {e.agent_id for e in acl}
        assert agent_ids == {"alice", "bob"}

    def test_notebook_auto_create(self, bus):
        bus.publish("notebook:alice", "alice", "text", "note")
        meta = bus.get_channel_meta("notebook:alice")
        assert meta is not None
        assert meta.visibility == "private"
        assert meta.owner == "alice"
        acl = bus.get_acl("notebook:alice")
        assert len(acl) == 1
        assert acl[0].agent_id == "alice"
        assert acl[0].permission == "admin"

    def test_memory_auto_create(self, bus):
        bus.publish("memory:alice", "alice", "text", "mem")
        meta = bus.get_channel_meta("memory:alice")
        assert meta is not None
        assert meta.visibility == "private"
        assert meta.owner == "alice"

    def test_broadcast_auto_create(self, bus):
        bus.publish("broadcast:news", "system", "text", "alert")
        meta = bus.get_channel_meta("broadcast:news")
        assert meta is not None
        assert meta.visibility == "public"

    def test_system_auto_create(self, bus):
        bus.publish("_system:events", "system", "text", "evt")
        meta = bus.get_channel_meta("_system:events")
        assert meta is not None
        assert meta.owner == "_system"
        assert meta.visibility == "public"

    def test_user_channel_no_auto_create(self, bus):
        bus.publish("general", "alice", "text", "hi")
        meta = bus.get_channel_meta("general")
        assert meta is None  # user channels are not auto-created

    def test_dm_idempotent(self, bus):
        bus.publish("dm:alice:bob", "alice", "text", "msg1")
        bus.publish("dm:alice:bob", "bob", "text", "msg2")
        # Should not raise or duplicate
        acl = bus.get_acl("dm:alice:bob")
        assert len(acl) == 2

    def test_dm_acl_enforced(self, bus):
        bus.publish("dm:alice:bob", "alice", "text", "hi")
        assert bus.check_access("dm:alice:bob", "alice", "write")
        assert bus.check_access("dm:alice:bob", "bob", "write")
        assert not bus.check_access("dm:alice:bob", "carol", "read")


# ──────────────────────────────────────────────
# SQLite backend integration
# ──────────────────────────────────────────────


class TestSQLiteBusACL:
    """ACL with SQLite backend for persistence verification."""

    def test_full_flow(self, sqlite_bus):
        sqlite_bus.create_channel("priv", "alice", visibility="private")
        entry = ACLEntry(channel="priv", agent_id="bob", permission="write", granted_at=_now())
        sqlite_bus.add_acl_entry(entry)
        assert sqlite_bus.check_access("priv", "bob", "write")
        assert not sqlite_bus.check_access("priv", "carol", "read")
        sqlite_bus.publish("priv", "bob", "text", "hello")
        msgs = sqlite_bus.query("priv")
        assert len(msgs) == 1

    def test_sugar_dm_sqlite(self, sqlite_bus):
        sqlite_bus.publish("dm:alice:bob", "alice", "text", "hi")
        meta = sqlite_bus.get_channel_meta("dm:alice:bob")
        assert meta.visibility == "private"
        acl = sqlite_bus.get_acl("dm:alice:bob")
        assert {e.agent_id for e in acl} == {"alice", "bob"}


# ──────────────────────────────────────────────
# ACL Enforcement at Bus Layer
# ──────────────────────────────────────────────


class TestACLEnforcement:
    """Bus-level ACL enforcement via opt-in parameters."""

    # ── publish ───────────────────────────────

    def test_publish_write_allowed(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry = ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at=_now())
        bus.add_acl_entry(entry)
        msg_id = bus.publish("ch", "bob", "text", "hi", enforce_acl=True)
        assert msg_id

    def test_publish_write_denied(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        with pytest.raises(PermissionError, match="write permission"):
            bus.publish("ch", "bob", "text", "hi", enforce_acl=True)

    def test_publish_owner_always_allowed(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        msg_id = bus.publish("ch", "alice", "text", "hi", enforce_acl=True)
        assert msg_id

    def test_publish_no_enforce_skips_check(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        # No ACL entry for bob, but enforce_acl=False (default)
        msg_id = bus.publish("ch", "bob", "text", "hi")
        assert msg_id

    def test_publish_public_channel_allows_write(self, bus):
        bus.create_channel("ch", "alice", visibility="public")
        msg_id = bus.publish("ch", "bob", "text", "hi", enforce_acl=True)
        assert msg_id

    # ── query ─────────────────────────────────

    def test_query_read_allowed(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry = ACLEntry(channel="ch", agent_id="bob", permission="read", granted_at=_now())
        bus.add_acl_entry(entry)
        bus.publish("ch", "alice", "text", "hi")
        msgs = bus.query("ch", agent_id="bob")
        assert len(msgs) == 1

    def test_query_read_denied(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        bus.publish("ch", "alice", "text", "hi")
        with pytest.raises(PermissionError, match="read permission"):
            bus.query("ch", agent_id="bob")

    def test_query_no_agent_skips_check(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        bus.publish("ch", "alice", "text", "hi")
        msgs = bus.query("ch")  # no agent_id
        assert len(msgs) == 1

    # ── subscribe ─────────────────────────────

    def test_subscribe_read_allowed(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry = ACLEntry(channel="ch", agent_id="bob", permission="read", granted_at=_now())
        bus.add_acl_entry(entry)
        sub_id = bus.subscribe("ch", lambda m: None, agent_id="bob")
        assert sub_id

    def test_subscribe_read_denied(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        with pytest.raises(PermissionError, match="read permission"):
            bus.subscribe("ch", lambda m: None, agent_id="bob")

    # ── delete_channel ────────────────────────

    def test_delete_channel_admin_allowed(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        bus.publish("ch", "alice", "text", "hi")
        count = bus.delete_channel("ch", agent_id="alice")  # owner = admin
        assert count == 1
        assert bus.get_channel_meta("ch") is None

    def test_delete_channel_admin_denied(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry = ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at=_now())
        bus.add_acl_entry(entry)
        with pytest.raises(PermissionError, match="admin permission"):
            bus.delete_channel("ch", agent_id="bob")

    def test_delete_channel_no_agent_skips_check(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        bus.publish("ch", "alice", "text", "hi")
        count = bus.delete_channel("ch")
        assert count == 1
        assert bus.get_channel_meta("ch") is None

    # ── delete_message ────────────────────────

    def test_delete_own_message(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry = ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at=_now())
        bus.add_acl_entry(entry)
        msg_id = bus.publish("ch", "bob", "text", "hi")
        assert bus.delete_message(msg_id, agent_id="bob")

    def test_delete_other_message_as_admin(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry = ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at=_now())
        bus.add_acl_entry(entry)
        msg_id = bus.publish("ch", "bob", "text", "hi")
        # alice is owner (admin) — can delete bob's message
        assert bus.delete_message(msg_id, agent_id="alice")

    def test_delete_other_message_denied(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry_b = ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at=_now())
        entry_c = ACLEntry(channel="ch", agent_id="carol", permission="write", granted_at=_now())
        bus.add_acl_entry(entry_b)
        bus.add_acl_entry(entry_c)
        msg_id = bus.publish("ch", "bob", "text", "hi")
        with pytest.raises(PermissionError, match="cannot delete"):
            bus.delete_message(msg_id, agent_id="carol")

    def test_delete_message_no_agent_skips_check(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        msg_id = bus.publish("ch", "alice", "text", "hi")
        assert bus.delete_message(msg_id)

    # ── unregistered channels (backward compat) ──

    def test_unregistered_channel_allows_all(self, bus):
        """Channels without metadata allow all operations."""
        msg_id = bus.publish("random", "anyone", "text", "hi", enforce_acl=True)
        assert msg_id
        msgs = bus.query("random", agent_id="anyone")
        assert len(msgs) == 1

    # ── permission hierarchy ──────────────────

    def test_write_implies_read(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry = ACLEntry(channel="ch", agent_id="bob", permission="write", granted_at=_now())
        bus.add_acl_entry(entry)
        # bob has write, should be able to read
        msgs = bus.query("ch", agent_id="bob")
        assert msgs == []

    def test_admin_implies_write(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry = ACLEntry(channel="ch", agent_id="bob", permission="admin", granted_at=_now())
        bus.add_acl_entry(entry)
        msg_id = bus.publish("ch", "bob", "text", "hi", enforce_acl=True)
        assert msg_id

    def test_read_does_not_imply_write(self, bus):
        bus.create_channel("ch", "alice", visibility="private")
        entry = ACLEntry(channel="ch", agent_id="bob", permission="read", granted_at=_now())
        bus.add_acl_entry(entry)
        with pytest.raises(PermissionError, match="write permission"):
            bus.publish("ch", "bob", "text", "hi", enforce_acl=True)


# ──────────────────────────────────────────────
# HTTP Endpoint Tests
# ──────────────────────────────────────────────


class TestHttpChannelACL:
    """HTTP endpoints for channel metadata and ACL management."""

    @pytest.fixture()
    def http_client(self):
        """Start an HTTP server with MemoryBackend, yield (base_url, bus)."""
        backend = MemoryBackend()
        bus = Bus(backend=backend)
        frontend = HttpFrontend(port=0)
        frontend.attach(bus)

        server_thread = threading.Thread(target=frontend.serve_forever, daemon=True)
        server_thread.start()

        for _ in range(50):
            if frontend._app.port is not None:
                break
            time.sleep(0.05)

        host, port = frontend.address
        base_url = f"http://{host}:{port}"
        yield base_url, bus
        frontend.shutdown()

    def _req(self, method, url, body=None):
        """Make HTTP request, return (status, data)."""
        import http.client
        import urllib.parse

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

    # ── Channel creation ──

    def test_create_channel(self, http_client):
        base, bus = http_client
        status, data = self._req(
            "POST",
            f"{base}/v1/channels",
            {
                "name": "team",
                "owner": "alice",
                "visibility": "private",
            },
        )
        assert status == 201
        assert data["channel"]["name"] == "team"
        assert data["channel"]["owner"] == "alice"
        assert data["channel"]["visibility"] == "private"

    def test_create_channel_missing_name(self, http_client):
        base, _ = http_client
        status, data = self._req("POST", f"{base}/v1/channels", {"owner": "a"})
        assert status == 400

    def test_create_channel_duplicate(self, http_client):
        base, _ = http_client
        self._req("POST", f"{base}/v1/channels", {"name": "ch", "owner": "a"})
        status, data = self._req("POST", f"{base}/v1/channels", {"name": "ch", "owner": "b"})
        assert status == 409

    # ── Channel metadata ──

    def test_get_channel_meta(self, http_client):
        base, bus = http_client
        bus.create_channel("info", "alice", visibility="public")
        status, data = self._req("GET", f"{base}/v1/channels/info/meta")
        assert status == 200
        assert data["channel"]["owner"] == "alice"

    def test_get_channel_meta_not_found(self, http_client):
        base, _ = http_client
        status, data = self._req("GET", f"{base}/v1/channels/nope/meta")
        assert status == 404

    # ── ACL CRUD ──

    def test_get_acl_empty(self, http_client):
        base, bus = http_client
        bus.create_channel("ch", "alice")
        status, data = self._req("GET", f"{base}/v1/channels/ch/acl")
        assert status == 200
        assert data["acl"] == []

    def test_add_acl_entry(self, http_client):
        base, bus = http_client
        bus.create_channel("ch", "alice")
        status, data = self._req(
            "POST",
            f"{base}/v1/channels/ch/acl",
            {
                "agent_id": "bob",
                "permission": "write",
            },
        )
        assert status == 201
        assert data["entry"]["agent_id"] == "bob"
        assert data["entry"]["permission"] == "write"

    def test_add_acl_bad_permission(self, http_client):
        base, bus = http_client
        bus.create_channel("ch", "alice")
        status, data = self._req(
            "POST",
            f"{base}/v1/channels/ch/acl",
            {
                "agent_id": "bob",
                "permission": "superadmin",
            },
        )
        assert status == 400

    def test_set_acl_replaces(self, http_client):
        base, bus = http_client
        bus.create_channel("ch", "alice")
        # Add initial entry
        self._req(
            "POST",
            f"{base}/v1/channels/ch/acl",
            {
                "agent_id": "bob",
                "permission": "read",
            },
        )
        # Replace with new set
        status, data = self._req(
            "PUT",
            f"{base}/v1/channels/ch/acl",
            {
                "acl": [
                    {"agent_id": "carol", "permission": "write"},
                    {"agent_id": "dave", "permission": "admin"},
                ],
            },
        )
        assert status == 200
        assert data["count"] == 2
        # Verify bob is gone
        _, acl_data = self._req("GET", f"{base}/v1/channels/ch/acl")
        agents = {e["agent_id"] for e in acl_data["acl"]}
        assert "bob" not in agents
        assert "carol" in agents
        assert "dave" in agents

    def test_remove_acl_entry(self, http_client):
        base, bus = http_client
        bus.create_channel("ch", "alice")
        self._req(
            "POST",
            f"{base}/v1/channels/ch/acl",
            {
                "agent_id": "bob",
                "permission": "read",
            },
        )
        status, data = self._req("DELETE", f"{base}/v1/channels/ch/acl/bob")
        assert status == 200

    def test_remove_acl_entry_not_found(self, http_client):
        base, bus = http_client
        bus.create_channel("ch", "alice")
        status, data = self._req("DELETE", f"{base}/v1/channels/ch/acl/nobody")
        assert status == 404
