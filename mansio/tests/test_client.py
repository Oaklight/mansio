"""Tests for MansioClient SDK."""

import json
from typing import Any

import pytest

from mansio import Bus, MansioClient, MemoryBackend
from mansio.transport import LocalTransport

# ── Helpers ───────────────────────────────────────────────────────


def _make_bus(*, require_auth: bool = False) -> Bus:
    """Create a Bus with MemoryBackend for testing."""
    return Bus(backend=MemoryBackend(), require_auth=require_auth)


def _make_client(
    bus: Bus | None = None,
    agent_id: str = "test-agent",
    **kwargs,
) -> MansioClient:
    """Create a MansioClient with sensible test defaults."""
    if bus is None:
        bus = _make_bus()
    return MansioClient(bus, agent_id, **kwargs)


# ──────────────────────────────────────────────────────────────────
# Transport
# ──────────────────────────────────────────────────────────────────


class TestLocalTransport:
    """Tests for LocalTransport."""

    def test_publish_delegates_to_bus(self):
        bus = _make_bus()
        transport = LocalTransport(bus)
        msg_id = transport.publish("ch", "sender", "text", "hello")
        assert isinstance(msg_id, str)
        # Verify message landed in bus
        msgs = bus.query("ch")
        assert len(msgs) == 1
        assert msgs[0].payload == "hello"
        bus.close()

    def test_query_delegates_to_bus(self):
        bus = _make_bus()
        transport = LocalTransport(bus)
        bus.publish("ch", "s", "text", "msg1")
        bus.publish("ch", "s", "text", "msg2")
        msgs = transport.query("ch", limit=10)
        assert len(msgs) == 2
        assert msgs[0].payload == "msg1"
        bus.close()

    def test_list_channels_delegates(self):
        bus = _make_bus()
        transport = LocalTransport(bus)
        assert transport.list_channels() == []
        bus.publish("alpha", "s", "text", "x")
        bus.publish("beta", "s", "text", "y")
        channels = transport.list_channels()
        assert "alpha" in channels
        assert "beta" in channels
        bus.close()

    def test_require_auth_false_by_default(self):
        bus = _make_bus()
        transport = LocalTransport(bus)
        assert transport.require_auth is False
        bus.close()

    def test_require_auth_true(self):
        bus = _make_bus(require_auth=True)
        transport = LocalTransport(bus)
        assert transport.require_auth is True
        bus.close()

    def test_close_is_noop(self):
        """Closing transport does NOT close the bus."""
        bus = _make_bus()
        transport = LocalTransport(bus)
        bus.publish("ch", "s", "text", "hello")
        transport.close()
        # Bus still works after transport close
        msgs = bus.query("ch")
        assert len(msgs) == 1
        bus.close()

    def test_repr(self):
        bus = _make_bus()
        transport = LocalTransport(bus)
        r = repr(transport)
        assert "LocalTransport" in r
        assert "Bus" in r
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Bus require_auth
# ──────────────────────────────────────────────────────────────────


class TestBusRequireAuth:
    """Tests for Bus.require_auth property."""

    def test_default_no_auth(self):
        bus = Bus(backend=MemoryBackend())
        assert bus.require_auth is False
        bus.close()

    def test_require_auth_true(self):
        bus = Bus(backend=MemoryBackend(), require_auth=True)
        assert bus.require_auth is True
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Identity Validation
# ──────────────────────────────────────────────────────────────────


class TestAgentIdValidation:
    """Tests for agent_id format validation."""

    def test_valid_simple(self):
        bus = _make_bus()
        client = MansioClient(bus, "coder-1")
        assert client.agent_id == "coder-1"
        client.close()

    def test_valid_all_lowercase(self):
        bus = _make_bus()
        client = MansioClient(bus, "abc")
        assert client.agent_id == "abc"
        client.close()

    def test_valid_with_numbers(self):
        bus = _make_bus()
        client = MansioClient(bus, "agent-42-x")
        assert client.agent_id == "agent-42-x"
        client.close()

    def test_valid_max_length(self):
        # 64 chars: starts and ends with alphanumeric
        agent_id = "a" + "-b" * 31 + "c"  # 1 + 62 + 1 = 64
        bus = _make_bus()
        client = MansioClient(bus, agent_id)
        assert client.agent_id == agent_id
        client.close()

    def test_reject_uppercase(self):
        bus = _make_bus()
        with pytest.raises(ValueError, match="Invalid agent_id"):
            MansioClient(bus, "Agent-1")

    def test_reject_too_short(self):
        bus = _make_bus()
        with pytest.raises(ValueError, match="Invalid agent_id"):
            MansioClient(bus, "ab")

    def test_reject_too_long(self):
        bus = _make_bus()
        agent_id = "a" * 65
        with pytest.raises(ValueError, match="Invalid agent_id"):
            MansioClient(bus, agent_id)

    def test_reject_starts_with_hyphen(self):
        bus = _make_bus()
        with pytest.raises(ValueError, match="Invalid agent_id"):
            MansioClient(bus, "-agent")

    def test_reject_ends_with_hyphen(self):
        bus = _make_bus()
        with pytest.raises(ValueError, match="Invalid agent_id"):
            MansioClient(bus, "agent-")

    def test_reject_special_chars(self):
        bus = _make_bus()
        with pytest.raises(ValueError, match="Invalid agent_id"):
            MansioClient(bus, "agent_one")

    def test_reject_spaces(self):
        bus = _make_bus()
        with pytest.raises(ValueError, match="Invalid agent_id"):
            MansioClient(bus, "agent one")

    def test_reject_empty(self):
        bus = _make_bus()
        with pytest.raises(ValueError, match="Invalid agent_id"):
            MansioClient(bus, "")


# ──────────────────────────────────────────────────────────────────
# Constructor
# ──────────────────────────────────────────────────────────────────


class TestMansioClientConstructor:
    """Tests for MansioClient constructor target resolution."""

    def test_bus_object_local_transport(self):
        """Bus -> LocalTransport, client does NOT own bus."""
        bus = _make_bus()
        client = MansioClient(bus, "test-agent")
        assert isinstance(client.transport, LocalTransport)
        client.close()
        # Bus still usable after client close
        bus.publish("ch", "s", "text", "still alive")
        assert len(bus.query("ch")) > 0
        bus.close()

    def test_memory_string_creates_bus(self):
        """:memory: -> auto-creates Bus, client owns bus."""
        client = MansioClient(":memory:", "test-agent")
        assert isinstance(client.transport, LocalTransport)
        client.close()

    def test_file_path_creates_bus(self, tmp_path):
        """File path -> auto-creates Bus with SQLiteBackend."""
        db_file = str(tmp_path / "test.db")
        client = MansioClient(db_file, "test-agent")
        assert isinstance(client.transport, LocalTransport)
        client.channel_send("ch", "hello")
        client.close()
        # DB file should exist
        assert (tmp_path / "test.db").exists()

    def test_http_uses_http_transport(self):
        from mansio.transport_http import HttpTransport

        stub: Any = type("_Stub", (), {"_agent_id": "test-agent", "_token": None})()
        t, bus, owns = MansioClient._resolve_target(stub, "http://localhost:9999")
        assert isinstance(t, HttpTransport)
        assert bus is None
        assert owns is False
        t.close()

    def test_https_uses_http_transport(self):
        from mansio.transport_http import HttpTransport

        stub: Any = type("_Stub", (), {"_agent_id": "test-agent", "_token": None})()
        t, bus, owns = MansioClient._resolve_target(stub, "https://mansio.example.com")
        assert isinstance(t, HttpTransport)
        assert bus is None
        assert owns is False
        t.close()

    def test_redis_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="redis"):
            MansioClient("redis://localhost:6379", "test-agent")

    def test_amqp_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="amqp"):
            MansioClient("amqp://localhost:5672", "test-agent")

    def test_invalid_target_type_raises(self):
        with pytest.raises(TypeError, match="target must be Bus or str"):
            MansioClient(42, "test-agent")  # type: ignore

    def test_display_name_defaults_to_agent_id(self):
        bus = _make_bus()
        client = MansioClient(bus, "test-agent")
        assert client.display_name == "test-agent"
        client.close()

    def test_display_name_custom(self):
        bus = _make_bus()
        client = MansioClient(bus, "test-agent", display_name="Test Bot")
        assert client.display_name == "Test Bot"
        client.close()

    def test_properties(self):
        bus = _make_bus()
        client = MansioClient(bus, "test-agent", display_name="My Agent")
        assert client.agent_id == "test-agent"
        assert client.display_name == "My Agent"
        assert isinstance(client.transport, LocalTransport)
        client.close()

    def test_repr(self):
        bus = _make_bus()
        client = MansioClient(bus, "test-agent")
        r = repr(client)
        assert "MansioClient" in r
        assert "test-agent" in r
        client.close()


# ──────────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────────


class TestMansioClientLifecycle:
    """Tests for close() and context manager."""

    def test_close_saves_cursors(self):
        bus = _make_bus()
        client = MansioClient(bus, "test-agent")
        # Create some cursor state via poll
        bus.publish("ch", "other", "text", "hello")
        client.channel_poll("ch")
        client.close()
        # Cursor snapshot should be in system channel
        cursor_msgs = bus.query("_system:cursors:test-agent")
        assert len(cursor_msgs) > 0
        assert cursor_msgs[-1].msg_type == "cursor_snapshot"
        bus.close()

    def test_context_manager(self):
        bus = _make_bus()
        with MansioClient(bus, "test-agent") as client:
            client.channel_send("ch", "hello")
        # After exit, cursors should be saved (or at least no error)
        bus.close()

    def test_owns_bus_closes_bus(self, tmp_path):
        """When client created bus from path, close() closes bus."""
        db_file = str(tmp_path / "test.db")
        client = MansioClient(db_file, "test-agent")
        client.channel_send("ch", "hello")
        client.close()
        # Bus is closed, but file persists

    def test_not_owns_bus_keeps_bus_open(self):
        """When client received Bus object, close() does NOT close bus."""
        bus = _make_bus()
        client = MansioClient(bus, "test-agent")
        client.close()
        # Bus should still work
        bus.publish("ch", "s", "text", "after close")
        msgs = bus.query("ch")
        assert any(m.payload == "after close" for m in msgs)
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Core API: channel_send
# ──────────────────────────────────────────────────────────────────


class TestChannelSend:
    """Tests for channel_send."""

    def test_send_returns_id(self):
        bus = _make_bus()
        client = _make_client(bus)
        msg_id = client.channel_send("ch", "hello")
        assert isinstance(msg_id, str)
        assert len(msg_id) > 0
        client.close()
        bus.close()

    def test_send_default_msg_type(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.channel_send("ch", "hello")
        msgs = bus.query("ch")
        assert msgs[0].msg_type == "chat"
        client.close()
        bus.close()

    def test_send_custom_msg_type(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.channel_send("ch", "hello", msg_type="notification")
        msgs = bus.query("ch")
        assert msgs[0].msg_type == "notification"
        client.close()
        bus.close()

    def test_send_with_metadata(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.channel_send("ch", "hello", metadata={"priority": "high"})
        msgs = bus.query("ch")
        assert msgs[0].metadata == {"priority": "high"}
        client.close()
        bus.close()

    def test_sender_is_agent_id(self):
        bus = _make_bus()
        client = _make_client(bus, agent_id="my-agent")
        client.channel_send("ch", "hello")
        msgs = bus.query("ch")
        assert msgs[0].sender == "my-agent"
        client.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Core API: channel_read
# ──────────────────────────────────────────────────────────────────


class TestChannelRead:
    """Tests for channel_read (no cursor advancement)."""

    def test_read_messages(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.channel_send("ch", "msg1")
        client.channel_send("ch", "msg2")
        msgs = client.channel_read("ch")
        assert len(msgs) == 2
        assert msgs[0].payload == "msg1"
        assert msgs[1].payload == "msg2"
        client.close()
        bus.close()

    def test_read_with_limit(self):
        bus = _make_bus()
        client = _make_client(bus)
        for i in range(5):
            client.channel_send("ch", f"msg{i}")
        msgs = client.channel_read("ch", limit=3)
        assert len(msgs) == 3
        client.close()
        bus.close()

    def test_read_with_after(self):
        bus = _make_bus()
        client = _make_client(bus)
        id1 = client.channel_send("ch", "msg1")
        client.channel_send("ch", "msg2")
        client.channel_send("ch", "msg3")
        msgs = client.channel_read("ch", after=id1)
        assert len(msgs) == 2
        assert msgs[0].payload == "msg2"
        client.close()
        bus.close()

    def test_read_empty_channel(self):
        bus = _make_bus()
        client = _make_client(bus)
        msgs = client.channel_read("nonexistent")
        assert msgs == []
        client.close()
        bus.close()

    def test_read_does_not_advance_cursor(self):
        """Repeated reads return same messages."""
        bus = _make_bus()
        client = _make_client(bus)
        client.channel_send("ch", "msg1")
        msgs1 = client.channel_read("ch")
        msgs2 = client.channel_read("ch")
        assert len(msgs1) == len(msgs2)
        assert msgs1[0].id == msgs2[0].id
        client.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Core API: channel_poll
# ──────────────────────────────────────────────────────────────────


class TestChannelPoll:
    """Tests for channel_poll (cursor auto-advances)."""

    def test_poll_returns_new_messages(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.channel_send("ch", "msg1")
        msgs = client.channel_poll("ch")
        # poll also gets system messages from _announce, but ch should have msg1
        assert any(m.payload == "msg1" for m in msgs)
        client.close()
        bus.close()

    def test_poll_advances_cursor(self):
        """Second poll returns empty if no new messages."""
        bus = _make_bus()
        client = _make_client(bus)
        client.channel_send("ch", "msg1")
        client.channel_poll("ch")
        msgs2 = client.channel_poll("ch")
        assert msgs2 == []
        client.close()
        bus.close()

    def test_poll_incremental(self):
        """Send more after first poll, second poll returns only new."""
        bus = _make_bus()
        client = _make_client(bus)
        client.channel_send("ch", "msg1")
        client.channel_poll("ch")
        client.channel_send("ch", "msg2")
        msgs = client.channel_poll("ch")
        assert len(msgs) == 1
        assert msgs[0].payload == "msg2"
        client.close()
        bus.close()

    def test_poll_empty_channel(self):
        bus = _make_bus()
        client = _make_client(bus)
        msgs = client.channel_poll("nonexistent")
        assert msgs == []
        client.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Core API: channel_list
# ──────────────────────────────────────────────────────────────────


class TestChannelList:
    """Tests for channel_list."""

    def test_list_includes_system_channels(self):
        """System channels from announce are visible."""
        bus = _make_bus()
        client = _make_client(bus)
        channels = client.channel_list()
        assert "_system:agents" in channels
        client.close()
        bus.close()

    def test_list_after_send(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.channel_send("my-channel", "hello")
        channels = client.channel_list()
        assert "my-channel" in channels
        client.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Sugar API: Notes
# ──────────────────────────────────────────────────────────────────


class TestNotes:
    """Tests for note_write / note_read."""

    def test_write_and_read(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.note_write("my first note")
        notes = client.note_read()
        assert len(notes) == 1
        assert notes[0].payload == "my first note"
        assert notes[0].msg_type == "note"
        client.close()
        bus.close()

    def test_write_with_tags(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.note_write("tagged note", tags=["python", "api"])
        notes = client.note_read()
        assert len(notes) == 1
        assert notes[0].metadata is not None
        assert notes[0].metadata["tags"] == ["python", "api"]
        client.close()
        bus.close()

    def test_read_filters_by_tags(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.note_write("note-a", tags=["python"])
        client.note_write("note-b", tags=["rust"])
        client.note_write("note-c", tags=["python", "web"])
        notes = client.note_read(tags=["python"])
        assert len(notes) == 2
        payloads = {n.payload for n in notes}
        assert payloads == {"note-a", "note-c"}
        client.close()
        bus.close()

    def test_read_excludes_non_note_types(self):
        """Thoughts in same channel are not returned."""
        bus = _make_bus()
        client = _make_client(bus)
        client.note_write("a note")
        client.thought_record("reasoning", "test", "thinking...")
        notes = client.note_read()
        assert len(notes) == 1
        assert notes[0].payload == "a note"
        client.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Sugar API: Thoughts
# ──────────────────────────────────────────────────────────────────


class TestThoughts:
    """Tests for thought_record / thought_read."""

    def test_record_and_read(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.thought_record("reasoning", "API design", "considering options...")
        thoughts = client.thought_read()
        assert len(thoughts) == 1
        assert thoughts[0].payload == "considering options..."
        assert thoughts[0].msg_type == "thought"
        client.close()
        bus.close()

    def test_metadata_fields(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.thought_record("planning", "architecture", "step 1: ...")
        thoughts = client.thought_read()
        assert thoughts[0].metadata is not None
        assert thoughts[0].metadata["thinking_mode"] == "planning"
        assert thoughts[0].metadata["focus_area"] == "architecture"
        client.close()
        bus.close()

    def test_read_excludes_notes(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.note_write("a note")
        client.thought_record("reflection", "review", "looks good")
        thoughts = client.thought_read()
        assert len(thoughts) == 1
        assert thoughts[0].msg_type == "thought"
        client.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Sugar API: Memory
# ──────────────────────────────────────────────────────────────────


class TestMemory:
    """Tests for memory_store / memory_recall."""

    def test_store_and_recall(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.memory_store("Python uses indentation for blocks")
        memories = client.memory_recall("Python")
        assert len(memories) == 1
        assert "Python" in memories[0].payload
        client.close()
        bus.close()

    def test_store_custom_type(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.memory_store("use pytest", memory_type="preference")
        msgs = client.channel_read(f"memory:{client.agent_id}")
        assert msgs[0].metadata is not None
        assert msgs[0].metadata["memory_type"] == "preference"
        client.close()
        bus.close()

    def test_recall_substring_match(self):
        bus = _make_bus()
        client = _make_client(bus)
        client.memory_store("Python is great")
        client.memory_store("Rust is fast")
        client.memory_store("Python and Rust")
        memories = client.memory_recall("rust")
        assert len(memories) == 2
        client.close()
        bus.close()

    def test_recall_limit(self):
        bus = _make_bus()
        client = _make_client(bus)
        for i in range(10):
            client.memory_store(f"memory item {i}")
        memories = client.memory_recall("memory", limit=3)
        assert len(memories) == 3
        client.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Sugar API: DM
# ──────────────────────────────────────────────────────────────────


class TestDM:
    """Tests for dm_send / dm_read."""

    def test_send_and_read(self):
        bus = _make_bus()
        alice = _make_client(bus, "alice-agent")
        bob = _make_client(bus, "bob-agent")
        alice.dm_send("bob-agent", "hi bob!")
        msgs = bob.dm_read("alice-agent")
        assert len(msgs) == 1
        assert msgs[0].payload == "hi bob!"
        assert msgs[0].sender == "alice-agent"
        alice.close()
        bob.close()
        bus.close()

    def test_channel_name_sorted(self):
        """dm:alice:bob same regardless of who initiates."""
        ch1 = MansioClient._dm_channel("zebra-agent", "alpha-agent")
        ch2 = MansioClient._dm_channel("alpha-agent", "zebra-agent")
        assert ch1 == ch2
        assert ch1 == "dm:alpha-agent:zebra-agent"

    def test_both_sides_see_messages(self):
        bus = _make_bus()
        alice = _make_client(bus, "alice-agent")
        bob = _make_client(bus, "bob-agent")
        alice.dm_send("bob-agent", "from alice")
        bob.dm_send("alice-agent", "from bob")
        alice_view = alice.dm_read("bob-agent")
        bob_view = bob.dm_read("alice-agent")
        assert len(alice_view) == len(bob_view) == 2
        alice.close()
        bob.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Sugar API: Broadcast
# ──────────────────────────────────────────────────────────────────


class TestBroadcast:
    """Tests for broadcast_list / broadcast_read."""

    def test_list_empty(self):
        bus = _make_bus()
        client = _make_client(bus)
        assert client.broadcast_list() == []
        client.close()
        bus.close()

    def test_list_shows_topics(self):
        bus = _make_bus()
        client = _make_client(bus)
        # Publish directly to broadcast channels via bus
        bus.publish("broadcast:announcements", "admin", "broadcast", "hello")
        bus.publish("broadcast:tasks", "admin", "broadcast", "task1")
        topics = client.broadcast_list()
        assert "announcements" in topics
        assert "tasks" in topics
        client.close()
        bus.close()

    def test_read_topic(self):
        bus = _make_bus()
        client = _make_client(bus)
        bus.publish("broadcast:news", "admin", "broadcast", "breaking news!")
        msgs = client.broadcast_read("news")
        assert len(msgs) == 1
        assert msgs[0].payload == "breaking news!"
        client.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Sugar API: Notifications
# ──────────────────────────────────────────────────────────────────


class TestNotifications:
    """Tests for notification_check."""

    def test_no_notifications(self):
        bus = _make_bus()
        client = _make_client(bus)
        notifs = client.notification_check()
        assert notifs == []
        client.close()
        bus.close()

    def test_check_returns_new(self):
        bus = _make_bus()
        client = _make_client(bus)
        # Send notification via bus
        bus.publish(
            f"_system:notifications:{client.agent_id}",
            "system",
            "notification",
            "you have mail",
        )
        notifs = client.notification_check()
        assert len(notifs) == 1
        assert notifs[0].payload == "you have mail"
        client.close()
        bus.close()

    def test_check_advances_cursor(self):
        bus = _make_bus()
        client = _make_client(bus)
        bus.publish(
            f"_system:notifications:{client.agent_id}",
            "system",
            "notification",
            "notif1",
        )
        client.notification_check()
        # Second check should be empty
        notifs = client.notification_check()
        assert notifs == []
        # New notification appears
        bus.publish(
            f"_system:notifications:{client.agent_id}",
            "system",
            "notification",
            "notif2",
        )
        notifs = client.notification_check()
        assert len(notifs) == 1
        assert notifs[0].payload == "notif2"
        client.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Cursor Persistence
# ──────────────────────────────────────────────────────────────────


class TestCursorPersistence:
    """Tests for cursor save/restore across sessions."""

    def test_cursors_saved_on_close(self):
        bus = _make_bus()
        client = MansioClient(bus, "test-agent")
        bus.publish("ch", "other", "text", "hello")
        client.channel_poll("ch")
        client.close()
        # Verify cursor snapshot exists
        cursor_msgs = bus.query("_system:cursors:test-agent")
        snapshots = [m for m in cursor_msgs if m.msg_type == "cursor_snapshot"]
        assert len(snapshots) > 0
        saved = json.loads(snapshots[-1].payload)
        assert "ch" in saved
        bus.close()

    def test_cursors_restored_on_reconnect(self):
        """Poll -> close -> new client -> poll returns only new."""
        bus = _make_bus()
        # Session 1: poll and close
        client1 = MansioClient(bus, "test-agent")
        bus.publish("ch", "other", "text", "msg1")
        client1.channel_poll("ch")
        client1.close()
        # New message arrives
        bus.publish("ch", "other", "text", "msg2")
        # Session 2: reconnect, poll should return only msg2
        client2 = MansioClient(bus, "test-agent")
        msgs = client2.channel_poll("ch")
        payloads = [m.payload for m in msgs]
        assert "msg2" in payloads
        assert "msg1" not in payloads
        client2.close()
        bus.close()

    def test_empty_cursors_not_saved(self):
        bus = _make_bus()
        client = MansioClient(bus, "test-agent")
        # Don't poll anything, so cursors remain empty
        client.close()
        cursor_msgs = bus.query("_system:cursors:test-agent")
        snapshots = [m for m in cursor_msgs if m.msg_type == "cursor_snapshot"]
        assert len(snapshots) == 0
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Authentication
# ──────────────────────────────────────────────────────────────────


class TestAuthentication:
    """Tests for register / auth flow."""

    def test_register_returns_client_and_secret(self):
        bus = _make_bus(require_auth=True)
        client, secret = MansioClient.register(bus, "test-agent")
        assert isinstance(client, MansioClient)
        assert isinstance(secret, str)
        assert client.agent_id == "test-agent"
        client.close()
        bus.close()

    def test_register_secret_format(self):
        """Secret starts with 'sk-'."""
        bus = _make_bus(require_auth=True)
        _, secret = MansioClient.register(bus, "test-agent")
        assert secret.startswith("sk-")
        assert len(secret) > 10
        bus.close()

    def test_reconnect_with_correct_secret(self):
        bus = _make_bus(require_auth=True)
        _, secret = MansioClient.register(bus, "test-agent")
        # Reconnect with same secret
        client = MansioClient(bus, "test-agent", secret=secret)
        assert client.agent_id == "test-agent"
        client.close()
        bus.close()

    def test_reconnect_with_wrong_secret(self):
        bus = _make_bus(require_auth=True)
        MansioClient.register(bus, "test-agent")
        with pytest.raises(PermissionError, match="Invalid secret"):
            MansioClient(bus, "test-agent", secret="sk-wrong")
        bus.close()

    def test_no_auth_mode_skips_validation(self):
        bus = _make_bus(require_auth=False)
        # No secret needed when auth not required
        client = MansioClient(bus, "test-agent")
        assert client.agent_id == "test-agent"
        client.close()
        bus.close()

    def test_auth_required_no_secret_raises(self):
        bus = _make_bus(require_auth=True)
        # Register first so agent exists
        MansioClient.register(bus, "test-agent")
        with pytest.raises(PermissionError, match="no secret provided"):
            MansioClient(bus, "test-agent")
        bus.close()

    def test_auth_required_unregistered_agent_raises(self):
        bus = _make_bus(require_auth=True)
        with pytest.raises(PermissionError, match="not registered"):
            MansioClient(bus, "test-agent", secret="sk-whatever")
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Reserved Methods
# ──────────────────────────────────────────────────────────────────


class TestReservedMethods:
    """Tests for not-yet-implemented methods."""

    def test_rotate_secret_raises(self):
        bus = _make_bus()
        client = _make_client(bus)
        with pytest.raises(NotImplementedError):
            client.rotate_secret()
        client.close()
        bus.close()

    def test_revoke_raises(self):
        bus = _make_bus()
        client = _make_client(bus)
        with pytest.raises(NotImplementedError):
            client.revoke()
        client.close()
        bus.close()


# ──────────────────────────────────────────────────────────────────
# Soft Registration
# ──────────────────────────────────────────────────────────────────


class TestSoftRegistration:
    """Tests for presence announcement on connect."""

    def test_announce_writes_to_system_agents(self):
        bus = _make_bus()
        client = MansioClient(bus, "test-agent")
        msgs = bus.query("_system:agents")
        assert len(msgs) >= 1
        presence_msgs = [m for m in msgs if m.sender == "test-agent"]
        assert len(presence_msgs) >= 1
        assert presence_msgs[0].msg_type == "presence"
        client.close()
        bus.close()

    def test_announce_includes_display_name(self):
        bus = _make_bus()
        client = MansioClient(bus, "test-agent", display_name="Test Bot")
        msgs = bus.query("_system:agents")
        presence_msgs = [m for m in msgs if m.sender == "test-agent"]
        assert presence_msgs[0].metadata is not None
        assert presence_msgs[0].metadata["display_name"] == "Test Bot"
        client.close()
        bus.close()
