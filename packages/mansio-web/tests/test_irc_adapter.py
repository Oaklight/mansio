"""Tests for IRC adapter — IrcAdapter.

Verifies:
- Adapter protocol compliance (attach, serve_forever, shutdown)
- Channel mapping (auto and explicit)
- Message bridging: IRC→mansio and mansio→IRC
- Echo prevention (IRC-originated messages not echoed back)
- Edge cases (double attach, serve without attach, nickname collision)
"""

from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock

import pytest
from mansio_core import Bus, MemoryBackend
from mansio_core.contracts import PublishRequest
from mansio_core.operations import OperationError
from mansio_core.principal import PrincipalKind
from mansio_core.token_store import TokenStore

# Guard: skip all tests if irc package is not installed
irc_bot = pytest.importorskip("irc.bot", reason="irc package not installed")

import mansio_web.adapters.irc as irc_module  # noqa: E402
from mansio_web.adapters.irc import IrcAdapter, _IrcBridgeBot  # noqa: E402

_LOGGER = "mansio_web.adapters.irc"


def _bot(adapter: IrcAdapter) -> _IrcBridgeBot:
    return _IrcBridgeBot(adapter, [("localhost", 6667)], "mansio-bot")


def _pubmsg(channel: str, nick: str, text: str, bot_nick: str = "mansio-bot"):
    event = MagicMock()
    event.target = channel
    event.source.nick = nick
    event.arguments = [text]
    connection = MagicMock()
    connection.get_nickname.return_value = bot_nick
    return connection, event


@pytest.fixture(autouse=True)
def _stop_revalidation_threads(monkeypatch: pytest.MonkeyPatch):
    """Wake every revalidation thread a test started, so none outlives it."""
    stops: list[threading.Event] = []
    original = IrcAdapter._start_revalidation

    def tracking(self):
        original(self)
        if self._revalidate_stop is not None:
            stops.append(self._revalidate_stop)

    monkeypatch.setattr(IrcAdapter, "_start_revalidation", tracking)
    yield
    for stop in stops:
        stop.set()


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
            channels=["alpha"],
            channel_map={"bravo": "#bravo"},
        )
        assert adapter._mansio_to_irc == {"alpha": "#alpha", "bravo": "#bravo"}
        assert adapter._irc_to_mansio == {"#alpha": "alpha", "#bravo": "bravo"}


# ── Construction Validation Tests ────────────────────────────────


class TestConstructionValidation:
    """Configuration the bridge can never use is refused at construction."""

    @pytest.mark.parametrize("channel", ["a", "Tasks", "foo:bar", "with space"])
    def test_invalid_channel_name_is_rejected(self, channel: str) -> None:
        with pytest.raises(ValueError, match="channel"):
            IrcAdapter(channels=[channel])

    def test_invalid_channel_map_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="channel"):
            IrcAdapter(channel_map={"a": "#alpha"})

    @pytest.mark.parametrize(
        "channel", ["dm:agent-1:agent-2", "notebook:agent-1", "memory:agent-1"]
    )
    def test_private_channels_cannot_be_configured(self, channel: str) -> None:
        with pytest.raises(ValueError, match="never bridges"):
            IrcAdapter(channels=[channel])

    def test_system_channels_cannot_be_configured(self) -> None:
        with pytest.raises(ValueError, match="never bridges"):
            IrcAdapter(channels=["_system:presence:agent-1"])

    def test_broadcast_channel_is_allowed(self) -> None:
        adapter = IrcAdapter(channels=["broadcast:all"])
        assert adapter._mansio_to_irc == {"broadcast:all": "#broadcast:all"}

    def test_empty_nick_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty nick"):
            IrcAdapter(channels=["tasks"], users={"": "alice-agent-1"})

    def test_the_bots_own_nick_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="own nick"):
            IrcAdapter(
                channels=["tasks"],
                nickname="mansio-bot",
                users={"Mansio-Bot": "alice-agent-1"},
            )

    def test_nicks_that_casemap_to_the_same_name_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate IRC nick"):
            IrcAdapter(
                channels=["tasks"],
                users={"Alice": "alice-agent-1", "alice": "alice-agent-2"},
            )

    def test_one_user_id_may_not_be_mapped_twice(self) -> None:
        with pytest.raises(ValueError, match="mapped to both"):
            IrcAdapter(
                channels=["tasks"],
                users={"alice": "alice-agent-1", "bob": "alice-agent-1"},
            )


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
            channels=["alpha", "bravo"],
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

    def test_stop_revalidation_warns_when_the_thread_does_not_exit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stuck check must be visible in the log, not silently abandoned."""

        class _StuckThread:
            def join(self, timeout=None):
                return None

            def is_alive(self):
                return True

        adapter = IrcAdapter(channels=["tasks"])
        adapter._revalidate_stop = threading.Event()
        adapter._revalidate_thread = _StuckThread()

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            adapter._stop_revalidation()

        assert "still alive" in caplog.text


# ── IRC → mansio Bridging Tests ──────────────────────────────────


class TestIrcToMansio:
    """Test message bridging from IRC to mansio."""

    def test_pubmsg_publishes_to_bus(self) -> None:
        """Public IRC message is published to the mapped mansio channel."""
        adapter = IrcAdapter(channels=["tasks"], allow_unauthenticated=True)
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


# ── Inbound Identity Tests ───────────────────────────────────────


class TestInboundIdentity:
    """Who an IRC PRIVMSG publishes as (M6d-2)."""

    def test_mapped_nick_publishes_through_the_publish_operation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mapped nick publishes as its user id, with the normal checks."""
        adapter = IrcAdapter(channels=["tasks"], users={"alice": "alice-agent-1"})
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        captured: dict = {}
        real_publish = irc_module.message_operations.publish

        def spy(ctx, principal, dto):
            captured["kind"] = principal.kind
            captured["user_id"] = principal.user_id
            captured["dto"] = dto
            return real_publish(ctx, principal, dto)

        monkeypatch.setattr(irc_module.message_operations, "publish", spy)

        connection, event = _pubmsg("#tasks", "alice", "hello world")
        _bot(adapter).on_pubmsg(connection, event)

        assert captured["kind"] is PrincipalKind.USER
        assert captured["user_id"] == "alice-agent-1"
        assert isinstance(captured["dto"], PublishRequest)
        assert captured["dto"].sender == "alice-agent-1"
        assert captured["dto"].metadata == {"source": "irc", "irc_nick": "alice"}

        messages = bus.query("tasks")
        assert len(messages) == 1
        assert messages[0].sender == "alice-agent-1"
        assert messages[0].payload == "hello world"

    def test_unmapped_nick_without_token_store_publishes_as_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The no-auth fallback is opt-in even without a token store."""
        adapter = IrcAdapter(channels=["tasks"], allow_unauthenticated=True)
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        captured: dict = {}
        real_publish = irc_module.message_operations.publish

        def spy(ctx, principal, dto):
            captured["kind"] = principal.kind
            captured["dto"] = dto
            return real_publish(ctx, principal, dto)

        monkeypatch.setattr(irc_module.message_operations, "publish", spy)

        connection, event = _pubmsg("#tasks", "bob", "hi")
        _bot(adapter).on_pubmsg(connection, event)

        assert captured["kind"] is PrincipalKind.NO_AUTH
        assert captured["dto"].sender == "bob"
        assert [m.sender for m in bus.query("tasks")] == ["bob"]

    def test_unmapped_nick_with_token_store_is_dropped(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With auth enabled an unknown nick cannot publish."""
        store = TokenStore(str(tmp_path / "tokens.db"))
        adapter = IrcAdapter(channels=["tasks"], token_store=store)
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection, event = _pubmsg("#tasks", "bob", "hi")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _bot(adapter).on_pubmsg(connection, event)

        assert bus.query("tasks") == []
        assert "unmapped nick bob" in caplog.text

    def test_mapped_nick_is_subject_to_channel_access(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A mapped user who cannot publish on the channel is dropped."""
        adapter = IrcAdapter(channels=["locked"], users={"alice": "alice-agent-9"})
        bus = Bus(backend=MemoryBackend())
        bus.create_channel("locked", owner="alice-agent-1", visibility="private")
        adapter.attach(bus)

        connection, event = _pubmsg("#locked", "alice", "hello")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _bot(adapter).on_pubmsg(connection, event)

        assert bus.query("locked") == []
        assert "publish rejected (forbidden)" in caplog.text

    def test_operation_error_is_logged_and_never_escapes(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A rejected publish is logged with kind and message, then dropped."""
        adapter = IrcAdapter(channels=["tasks"], users={"alice": "alice-agent-1"})
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        def reject(ctx, principal, dto):
            raise OperationError("forbidden", "Token for 'alice-agent-1' cannot publish")

        monkeypatch.setattr(irc_module.message_operations, "publish", reject)

        connection, event = _pubmsg("#tasks", "alice", "hello")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _bot(adapter).on_pubmsg(connection, event)

        assert bus.query("tasks") == []
        assert "publish rejected (forbidden)" in caplog.text
        assert "cannot publish" in caplog.text

    @pytest.mark.parametrize("user_id", ["", "Bad ID", "ALICE", "alice_agent", "ab"])
    def test_invalid_mapped_user_id_fails_at_construction(self, user_id: str) -> None:
        """A mapping that could never be a mansio identity is refused."""
        with pytest.raises(ValueError, match="alice"):
            IrcAdapter(channels=["tasks"], users={"alice": user_id})

    def test_allow_unauthenticated_is_opt_in_and_needs_no_token_store(self, tmp_path) -> None:
        store = TokenStore(str(tmp_path / "tokens.db"))
        assert IrcAdapter(channels=["tasks"])._allow_unauthenticated is False
        assert (
            IrcAdapter(channels=["tasks"], allow_unauthenticated=True)._allow_unauthenticated
            is True
        )
        with pytest.raises(ValueError, match="cannot be combined"):
            IrcAdapter(channels=["tasks"], token_store=store, allow_unauthenticated=True)

    def test_nick_lookup_uses_irc_casemapping(self) -> None:
        adapter = IrcAdapter(channels=["tasks"], users={"Alice": "alice-agent-1"})
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection, event = _pubmsg("#tasks", "aLiCe", "hello")
        _bot(adapter).on_pubmsg(connection, event)

        assert [m.sender for m in bus.query("tasks")] == ["alice-agent-1"]

    def test_the_bots_own_nick_comparison_uses_casemapping(self) -> None:
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection, event = _pubmsg("#tasks", "mansio-bot", "hi", bot_nick="Mansio-Bot")
        _bot(adapter).on_pubmsg(connection, event)

        assert bus.query("tasks") == []

    def test_no_auth_fallback_can_be_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = IrcAdapter(channels=["tasks"], allow_unauthenticated=False)
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection, event = _pubmsg("#tasks", "bob", "hi")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _bot(adapter).on_pubmsg(connection, event)

        assert bus.query("tasks") == []
        assert "dropping message from unmapped nick bob" in caplog.text

    def test_no_auth_fallback_is_logged_per_message(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = IrcAdapter(channels=["tasks"], allow_unauthenticated=True)
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection, event = _pubmsg("#tasks", "bob", "hi")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _bot(adapter).on_pubmsg(connection, event)

        assert "unmapped nick bob publishing unauthenticated to tasks" in caplog.text

    def test_nick_change_to_a_mapped_nick_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = IrcAdapter(channels=["tasks"], users={"alice": "alice-agent-1"})
        event = MagicMock()
        event.source.nick = "newcomer"
        event.target = "Alice"

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _bot(adapter).on_nick(MagicMock(), event)

        assert "alice-agent-1" in caplog.text
        assert "Alice" in caplog.text

    def test_nick_change_from_a_mapped_nick_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = IrcAdapter(channels=["tasks"], users={"alice": "alice-agent-1"})
        event = MagicMock()
        event.source.nick = "alice"
        event.target = "alice_"

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _bot(adapter).on_nick(MagicMock(), event)

        assert "alice-agent-1" in caplog.text
        assert "alice_" in caplog.text

    def test_nick_change_between_unmapped_nicks_is_not_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = IrcAdapter(channels=["tasks"], users={"alice": "alice-agent-1"})
        event = MagicMock()
        event.source.nick = "someone"
        event.target = "someone_else"

        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _bot(adapter).on_nick(MagicMock(), event)

        assert "nick change" not in caplog.text

    def test_attach_builds_one_operation_context(self) -> None:
        """The adapter keeps a single OperationContext for every operation."""
        adapter = IrcAdapter(channels=["tasks"], users={"alice": "alice-agent-1"})
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        context = adapter._operations
        assert context is not None
        assert context.bus is bus
        assert adapter._operations is context


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


# ── Outbound Visibility Tests ────────────────────────────────────


class TestOutboundVisibility:
    """Which channels reach IRC, and for how long (M6d-2)."""

    def test_unregistered_channel_is_bridged(self) -> None:
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)

        assert list(adapter._sub_ids) == ["tasks"]
        bus.publish(channel="tasks", sender="agent-a", msg_type="text", payload="hi")
        connection.privmsg.assert_called_once_with("#tasks", "agent-a: hi")

    def test_public_channel_is_bridged(self) -> None:
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        bus.create_channel("tasks", owner="agent-a", visibility="public")
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)

        assert list(adapter._sub_ids) == ["tasks"]

    def test_private_channel_is_never_bridged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Not even when every mapped user may read it: IRC has other members."""
        adapter = IrcAdapter(channels=["locked"], users={"alice": "agent-1"})
        bus = Bus(backend=MemoryBackend())
        bus.create_channel("locked", owner="agent-1", visibility="private")
        adapter.attach(bus)

        connection = MagicMock()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            adapter._setup_bus_subscriptions(connection)
        adapter._stop_revalidation()

        assert adapter._sub_ids == {}
        assert "Not bridging mansio channel 'locked'" in caplog.text

        bus.publish(channel="locked", sender="agent-1", msg_type="text", payload="secret")
        connection.privmsg.assert_not_called()

    def test_broadcast_channel_is_bridged(self) -> None:
        adapter = IrcAdapter(channels=["broadcast:all"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)

        assert list(adapter._sub_ids) == ["broadcast:all"]

    def test_visibility_is_recomputed_on_reconnect(self) -> None:
        """A channel that turned private is dropped at the next setup."""
        adapter = IrcAdapter(channels=["later"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        conn1 = MagicMock()
        adapter._setup_bus_subscriptions(conn1)
        assert list(adapter._sub_ids) == ["later"]  # unregistered → public

        bus.create_channel("later", owner="agent-1", visibility="private")
        conn2 = MagicMock()
        adapter._setup_bus_subscriptions(conn2)
        adapter._stop_revalidation()

        assert adapter._sub_ids == {}
        bus.publish(channel="later", sender="agent-1", msg_type="text", payload="now private")
        conn1.privmsg.assert_not_called()
        conn2.privmsg.assert_not_called()

    def test_revalidation_unsubscribes_a_channel_that_turns_private(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)
        adapter._stop_revalidation()
        assert list(adapter._sub_ids) == ["tasks"]

        bus.create_channel("tasks", owner="agent-a", visibility="private")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            adapter._revalidate_channels()

        assert adapter._sub_ids == {}
        assert "no longer public" in caplog.text

        bus.publish(channel="tasks", sender="agent-a", msg_type="text", payload="after")
        connection.privmsg.assert_not_called()

    def test_revalidation_keeps_a_channel_when_the_check_raises(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A transient read error is not a revocation signal."""
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)
        adapter._stop_revalidation()

        def boom(ctx, channel):
            raise RuntimeError("backend down")

        monkeypatch.setattr(irc_module.streams, "bridgeable", boom)
        with caplog.at_level(logging.ERROR, logger=_LOGGER):
            adapter._revalidate_channels()

        assert list(adapter._sub_ids) == ["tasks"]
        assert "keeping it" in caplog.text

    def test_revalidation_survives_an_unsubscribe_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed unsubscribe keeps the handle and does not stop the tick."""
        adapter = IrcAdapter(channels=["tasks", "sync"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)
        adapter._stop_revalidation()
        assert list(adapter._sub_ids) == ["tasks", "sync"]

        bus.create_channel("tasks", owner="agent-a", visibility="private")
        bus.create_channel("sync", owner="agent-a", visibility="private")

        real_unsubscribe = irc_module.streams.unsubscribe
        calls = {"n": 0}

        def flaky(ctx, sub_ids):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("backend unwatch failed")
            real_unsubscribe(ctx, sub_ids)

        monkeypatch.setattr(irc_module.streams, "unsubscribe", flaky)
        with caplog.at_level(logging.ERROR, logger=_LOGGER):
            adapter._revalidate_channels()

        # The first channel kept its handle; the loop still processed the next.
        assert list(adapter._sub_ids) == ["tasks"]
        assert "could not unsubscribe 'tasks'" in caplog.text

    def test_long_payload_is_truncated_within_the_irc_limit(self) -> None:
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)

        bus.publish(channel="tasks", sender="agent-a", msg_type="text", payload="é" * 400)

        text = connection.privmsg.call_args[0][1]
        encoded = text.encode("utf-8")
        assert len(encoded) <= 400
        assert text.endswith("...")
        assert text.startswith("agent-a: é")

    def test_echo_prevention_survives_the_bridged_subscription(self) -> None:
        """Still skipped: the metadata marker, not the identity mapping."""
        adapter = IrcAdapter(channels=["tasks"], users={"alice": "alice-agent-1"})
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)

        bus.publish(
            channel="tasks",
            sender="alice-agent-1",
            msg_type="text",
            payload="from irc",
            metadata={"source": "irc", "irc_nick": "alice"},
        )

        connection.privmsg.assert_not_called()

    def test_privmsg_is_serialized_by_the_connection_lock(self) -> None:
        """Publisher threads and the reactor share one connection under a lock."""
        adapter = IrcAdapter(channels=["tasks"])
        bus = Bus(backend=MemoryBackend())
        adapter.attach(bus)

        connection = MagicMock()
        adapter._setup_bus_subscriptions(connection)
        adapter._stop_revalidation()

        entered = threading.Event()
        release = threading.Event()

        def slow_privmsg(*_args):
            entered.set()
            release.wait(2)

        connection.privmsg.side_effect = slow_privmsg

        adapter._connection_lock.acquire()
        try:
            worker = threading.Thread(
                target=lambda: bus.publish(
                    channel="tasks", sender="agent-a", msg_type="text", payload="hi"
                )
            )
            worker.start()
            assert not entered.wait(0.2)  # blocked behind the lock
        finally:
            adapter._connection_lock.release()

        assert entered.wait(2)
        release.set()
        worker.join(2)


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
