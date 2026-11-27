"""Tests for IRC adapter — IrcAdapter.

Verifies:
- Adapter protocol compliance (attach, serve_forever, shutdown)
- Channel mapping (auto and explicit)
- Message bridging: IRC→mansio and mansio→IRC
- Echo prevention (IRC-originated messages not echoed back)
- Edge cases (double attach, serve without attach, nickname collision)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mansio_core import Bus, MemoryBackend

# Guard: skip all tests if irc package is not installed
irc_bot = pytest.importorskip("irc.bot", reason="irc package not installed")

from mansio_web.adapters.irc import IrcAdapter, _IrcBridgeBot  # noqa: E402

# ── Channel Mapping Tests ────────────────────────────────────────


class TestChannelMapping:
    """Test IRC ↔ mansio channel name mapping."""

    def test_auto_mapping_adds_hash(self) -> None:
        """Channels auto-map to #<name> on IRC."""
        adapter = IrcAdapter(channels=["tasks", "sync"])
        assert adapter._mansio_to_irc == {"tasks": "#tasks", "sync": "#sync"}
        assert adapter._irc_to_mansio == {"#tasks": "tasks", "#sync": "sync"}

    def test_explicit_mapping_overrides_auto(self) -> None:
        """channel_map overrides auto-mapping for specified channels."""
        adapter = IrcAdapter(
            channels=["tasks", "sync"],
            channel_map={"tasks": "#project-tasks"},
        )
        assert adapter._mansio_to_irc["tasks"] == "#project-tasks"
        assert adapter._mansio_to_irc["sync"] == "#sync"
        assert adapter._irc_to_mansio["#project-tasks"] == "tasks"

    def test_channel_map_only(self) -> None:
        """channel_map entries not in channels list are still mapped."""
        adapter = IrcAdapter(channel_map={"extra": "#extra-channel"})
        assert adapter._mansio_to_irc["extra"] == "#extra-channel"
        assert adapter._irc_to_mansio["#extra-channel"] == "extra"

    def test_empty_channels(self) -> None:
        """No channels configured — empty mappings."""
        adapter = IrcAdapter()
        assert adapter._mansio_to_irc == {}
        assert adapter._irc_to_mansio == {}

    def test_combined_channels_and_map(self) -> None:
        """Channels + channel_map with disjoint entries."""
        adapter = IrcAdapter(
            channels=["a"],
            channel_map={"b": "#bravo"},
        )
        assert adapter._mansio_to_irc == {"a": "#a", "b": "#bravo"}
        assert adapter._irc_to_mansio == {"#a": "a", "#bravo": "b"}


# ── Adapter Protocol Tests ──────────────────────────────────────


class TestAdapterProtocol:
    """Test Adapter protocol compliance."""

    def test_attach_stores_bus(self) -> None:
        """attach() stores the bus reference."""
        adapter = IrcAdapter(channels=["test"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)
        assert adapter._bus is bus

    def test_double_attach_raises(self) -> None:
        """attach() raises RuntimeError if already attached."""
        adapter = IrcAdapter(channels=["test"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)
        with pytest.raises(RuntimeError, match="already attached"):
            adapter.attach(bus)

    def test_serve_without_attach_raises(self) -> None:
        """serve_forever() raises RuntimeError if not attached."""
        adapter = IrcAdapter(channels=["test"])
        with pytest.raises(RuntimeError, match="Must call attach"):
            adapter.serve_forever()

    def test_address_returns_host_port(self) -> None:
        """address property returns configured (host, port)."""
        adapter = IrcAdapter(irc_host="irc.example.com", irc_port=6697)
        assert adapter.address == ("irc.example.com", 6697)

    def test_repr_detached(self) -> None:
        """repr shows detached state."""
        adapter = IrcAdapter(
            irc_host="irc.example.com",
            irc_port=6667,
            nickname="testbot",
            channels=["a", "b"],
        )
        r = repr(adapter)
        assert "irc.example.com:6667" in r
        assert "testbot" in r
        assert "2 ch" in r
        assert "detached" in r

    def test_repr_attached(self) -> None:
        """repr shows attached state."""
        adapter = IrcAdapter(channels=["test"])
        adapter.attach(Bus(backend=MemoryBackend()))
        assert "attached" in repr(adapter)

    def test_shutdown_without_bot_is_safe(self) -> None:
        """shutdown() is safe to call before serve_forever() or multiple times."""
        adapter = IrcAdapter(channels=["test"])
        adapter.shutdown()  # should not raise
        adapter.shutdown()  # safe to call twice


# ── IRC → mansio Bridging Tests ──────────────────────────────────


class TestIrcToMansio:
    """Test message bridging from IRC to mansio."""

    def test_pubmsg_publishes_to_bus(self) -> None:
        """Public IRC message is published to the mapped mansio channel."""
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        # Create bot with mocked connection
        bot = _IrcBridgeBot(adapter, [("localhost", 6667)], "mansio-bot")

        # Simulate a PUBMSG event
        event = MagicMock()
        event.target = "#tasks"
        event.source.nick = "alice"
        event.arguments = ["hello world"]

        connection = MagicMock()
        connection.get_nickname.return_value = "mansio-bot"

        bot.on_pubmsg(connection, event)

        # Verify message was published to bus
        msgs = bus.query("tasks")
        assert len(msgs) == 1
        assert msgs[0].sender == "alice"
        assert msgs[0].payload == "hello world"
        assert msgs[0].msg_type == "text"
        assert msgs[0].metadata is not None
        assert msgs[0].metadata["source"] == "irc"
        assert msgs[0].metadata["irc_nick"] == "alice"

    def test_pubmsg_ignores_own_messages(self) -> None:
        """Bot's own messages are not bridged to mansio."""
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        bot = _IrcBridgeBot(adapter, [("localhost", 6667)], "mansio-bot")

        event = MagicMock()
        event.target = "#tasks"
        event.source.nick = "mansio-bot"  # bot's own nick
        event.arguments = ["echo message"]

        connection = MagicMock()
        connection.get_nickname.return_value = "mansio-bot"

        bot.on_pubmsg(connection, event)

        msgs = bus.query("tasks")
        assert len(msgs) == 0

    def test_pubmsg_ignores_unmapped_channel(self) -> None:
        """Messages from unmapped IRC channels are ignored."""
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        bot = _IrcBridgeBot(adapter, [("localhost", 6667)], "mansio-bot")

        event = MagicMock()
        event.target = "#random"  # not mapped
        event.source.nick = "alice"
        event.arguments = ["hello"]

        connection = MagicMock()
        connection.get_nickname.return_value = "mansio-bot"

        bot.on_pubmsg(connection, event)

        # Should not publish to any channel
        assert bus.query("random") == []
        assert bus.query("tasks") == []

    @pytest.mark.parametrize("text", ["", "   "])
    def test_pubmsg_ignores_empty_payload(self, text: str) -> None:
        """An empty PRIVMSG is not published: the wire contract rejects it."""
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        bot = _IrcBridgeBot(adapter, [("localhost", 6667)], "mansio-bot")

        event = MagicMock()
        event.target = "#tasks"
        event.source.nick = "alice"
        event.arguments = [text]

        connection = MagicMock()
        connection.get_nickname.return_value = "mansio-bot"

        bot.on_pubmsg(connection, event)

        assert bus.query("tasks") == []


# ── mansio → IRC Bridging Tests ──────────────────────────────────


class TestMansioToIrc:
    """Test message bridging from mansio to IRC."""

    def test_bus_message_forwarded_to_irc(self) -> None:
        """Messages published to bus are forwarded to IRC channel."""
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)

        # Publish a message to the bus (not from IRC)
        bus.publish(
            channel="tasks",
            sender="agent-a",
            msg_type="text",
            payload="task completed",
        )

        # Verify IRC message was sent
        connection.privmsg.assert_called_once_with("#tasks", "agent-a: task completed")

    def test_irc_originated_message_not_echoed(self) -> None:
        """Messages with source=irc metadata are not forwarded back to IRC."""
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)

        # Publish a message that originated from IRC
        bus.publish(
            channel="tasks",
            sender="alice",
            msg_type="text",
            payload="hello from irc",
            metadata={"source": "irc", "irc_nick": "alice"},
        )

        # Should NOT be forwarded to IRC
        connection.privmsg.assert_not_called()

    def test_multiple_channels_subscribed(self) -> None:
        """All mapped channels get bus subscriptions."""
        adapter = IrcAdapter(channels=["tasks", "sync"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)

        assert len(adapter._sub_ids) == 2

        bus.publish(channel="tasks", sender="agent-a", msg_type="text", payload="task msg")
        bus.publish(channel="sync", sender="agent-b", msg_type="text", payload="sync msg")

        assert connection.privmsg.call_count == 2
        calls = connection.privmsg.call_args_list
        call_args = {c[0] for c in calls}
        assert ("#tasks", "agent-a: task msg") in call_args
        assert ("#sync", "agent-b: sync msg") in call_args

    def test_reconnect_clears_old_subscriptions(self) -> None:
        """Calling _setup_bus_subscriptions again clears old subs first."""
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        conn1 = MagicMock()
        adapter._setup_bus_subscriptions(conn1)
        assert len(adapter._sub_ids) == 1

        # Simulate reconnect: setup called again with new connection
        conn2 = MagicMock()
        adapter._setup_bus_subscriptions(conn2)
        # Should still be 1, not 2 (old sub cleared)
        assert len(adapter._sub_ids) == 1

        # Publish should only forward once (to conn2, not conn1)
        bus.publish(channel="tasks", sender="agent-a", msg_type="text", payload="msg")
        conn1.privmsg.assert_not_called()
        conn2.privmsg.assert_called_once_with("#tasks", "agent-a: msg")

    def test_shutdown_unsubscribes_from_bus(self) -> None:
        """shutdown() removes all bus subscriptions."""
        adapter = IrcAdapter(channels=["tasks", "sync"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)
        assert len(adapter._sub_ids) == 2

        adapter.shutdown()
        assert len(adapter._sub_ids) == 0


# ── Bot Event Tests ──────────────────────────────────────────────


class TestBotEvents:
    """Test internal bot event handlers."""

    def test_on_welcome_joins_channels(self) -> None:
        """Bot joins all mapped IRC channels on connect."""
        adapter = IrcAdapter(channels=["tasks", "sync"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        bot = _IrcBridgeBot(adapter, [("localhost", 6667)], "mansio-bot")

        connection = MagicMock()
        connection.server = "localhost"
        connection.get_nickname.return_value = "mansio-bot"
        event = MagicMock()

        bot.on_welcome(connection, event)

        # Verify channels were joined
        join_calls = [c[0][0] for c in connection.join.call_args_list]
        assert "#tasks" in join_calls
        assert "#sync" in join_calls

    def test_on_nicknameinuse_appends_underscore(self) -> None:
        """Nickname collision appends underscore."""
        adapter = IrcAdapter(channels=["tasks"])

        bot = _IrcBridgeBot(adapter, [("localhost", 6667)], "mansio-bot")

        connection = MagicMock()
        connection.get_nickname.return_value = "mansio-bot"
        event = MagicMock()

        bot.on_nicknameinuse(connection, event)
        connection.nick.assert_called_once_with("mansio-bot_")


# ── Import Guard Tests ───────────────────────────────────────────


class TestImportGuard:
    """Test lazy import behavior in adapters package."""

    def test_lazy_import_irc_adapter(self) -> None:
        """IrcAdapter is importable from mansio_web.adapters."""
        from mansio_web.adapters import IrcAdapter

        assert IrcAdapter is not None

    def test_lazy_import_unknown_raises(self) -> None:
        """Unknown attributes raise AttributeError."""
        with pytest.raises(AttributeError, match="has no attribute"):
            from mansio_web import adapters

            adapters.__getattr__("NonExistentAdapter")
