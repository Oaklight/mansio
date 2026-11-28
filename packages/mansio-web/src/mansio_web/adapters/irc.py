"""IRC adapter for mansio — bridges a Bus to an IRC server.

Allows agents to communicate via IRC channels mapped to mansio channels.
IRC users can observe and participate in agent communication using any
standard IRC client.

Requires the ``irc`` PyPI package: ``pip install mansio[irc]``

Channel mapping:
    IRC ``#tasks`` ↔ mansio ``tasks`` (auto-strips/adds ``#`` prefix).
    Explicit mappings can override the default via *channel_map*.

Visibility:
    IRC is a broadcast medium: its members change at any time and the bridge
    cannot filter by recipient. Only public mansio channels are bridged.
    DMs, notebooks, memory channels, and the ``_system:`` namespace are never
    bridged. ``broadcast:`` channels are public by definition and therefore
    become world-readable over IRC.

    ``streams.bridgeable`` decides at subscribe time and is re-checked every
    five seconds on a daemon thread, so a channel that turns private stops
    being forwarded shortly after. One message can still slip through if a
    publish races a failing check. A channel skipped at connect is retried
    only on reconnect, not by the revalidation loop.

    Prefix checks are case-sensitive because channel names are lowercase by
    contract; a mixed-case ``DM:`` name is an ordinary public channel to the
    bus, so the bridge treats it the same way.

Identity:
    ``users`` maps an IRC nick to a mansio user id. A mapped nick publishes
    through the same rules as an API caller: the message sender is the
    mapped id, and the ACL, registered-prefix, and DM checks apply.
    An unmapped nick publishes as itself only when the adapter has no token
    store and was built with ``allow_unauthenticated=True``; with a token
    store the message is logged and dropped.

    Nicks are folded with RFC1459 casemapping for the map keys and every
    lookup, whatever CASEMAPPING the server announces.

    The map is authorization, not authentication: this adapter does not log
    in to the IRC server (no SASL), so the operator's server must guarantee
    that a nick belongs to the agent it names. A nick that changes to a
    mapped nick inherits that identity under the same assumption, and every
    such change is logged.

Message flow:
    IRC → mansio: PRIVMSG in a bridged channel calls
    :func:`mansio_core.operations.messages.publish`.
    mansio → IRC: a raw subscription forwards new messages as IRC PRIVMSGs.

ACL changes have no effect on the bridge: only public channels are bridged.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Any

try:
    import irc.bot
    import irc.connection
    import irc.strings
except ImportError as exc:
    raise ImportError(
        "IrcAdapter requires the 'irc' package. Install it with: pip install mansio[irc]"
    ) from exc

from mansio_core.authz import ChannelAuthorizer, is_private_channel
from mansio_core.contracts import PublishRequest
from mansio_core.operations import OperationContext, OperationError, streams
from mansio_core.operations import messages as message_operations
from mansio_core.operations.validation import validate_channel_name
from mansio_core.principal import Principal
from mansio_core.types import is_valid_user_id

if TYPE_CHECKING:
    from mansio_core.bus import Bus
    from mansio_core.token_store import TokenStore

logger = logging.getLogger(__name__)

# Maximum IRC message length (RFC 2812: 512 bytes including CRLF)
_MAX_IRC_LINE = 400  # leave room for protocol overhead

# Cursor and presence channels; unregistered, so visibility alone would not
# keep them off the bridge.
_SYSTEM_PREFIX = "_system:"

# How often a bridged channel is re-checked against the visibility rule.
_REVALIDATE_SECONDS = 5.0


def _casemap(nick: str) -> str:
    """Normalize *nick* with the IRC casemapping the client library uses."""
    return irc.strings.lower(nick)


class _IrcBridgeBot(irc.bot.SingleServerIRCBot):
    """Internal IRC bot that bridges messages between IRC and a mansio Bus.

    Args:
        adapter: The owning IrcAdapter instance.
        server_list: IRC server list for SingleServerIRCBot.
        nickname: Bot nickname on IRC.
        password: Optional server password.
    """

    def __init__(
        self,
        adapter: IrcAdapter,
        server_list: list[tuple[str, int]],
        nickname: str,
        password: str | None = None,
    ) -> None:
        connect_params: dict = {}
        if adapter._use_ssl:
            connect_params["connect_factory"] = irc.connection.Factory(wrapper=self._ssl_wrapper)

        super().__init__(
            server_list,
            nickname,
            nickname,  # realname = nickname
            **connect_params,
        )
        self._adapter = adapter
        self._password = password

    @staticmethod
    def _ssl_wrapper(sock):
        """Wrap socket with SSL for secure connections."""
        import ssl

        ctx = ssl.create_default_context()
        return ctx.wrap_socket(sock)

    def on_welcome(self, connection, event):
        """Called when the bot connects to the IRC server."""
        logger.info("IRC bot connected to %s as %s", connection.server, connection.get_nickname())

        # Join all mapped IRC channels
        for irc_channel in self._adapter._mansio_to_irc.values():
            logger.info("Joining IRC channel %s", irc_channel)
            connection.join(irc_channel)

        # Set up bus subscriptions for mansio→IRC forwarding
        self._adapter._setup_bus_subscriptions(connection)

    def on_pubmsg(self, connection, event):
        """Handle public messages in IRC channels (IRC → mansio)."""
        irc_channel = event.target
        nick = event.source.nick
        text = event.arguments[0] if event.arguments else ""

        # The wire contract rejects an empty payload, so publishing one would
        # store a message that no SSE subscriber can parse.  Drop it here.
        if not text.strip():
            logger.debug("IRC→mansio: dropping empty PRIVMSG on %s", irc_channel)
            return

        # Don't bridge our own messages
        if _casemap(nick) == _casemap(connection.get_nickname()):
            return

        adapter = self._adapter
        mansio_channel = adapter._irc_to_mansio.get(irc_channel)
        operations = adapter._operations
        if mansio_channel is None or operations is None:
            return

        # The nick is resolved per message, so a nick change to an unmapped
        # name stops publishing until it maps again.
        mapped_user = adapter._users.get(_casemap(nick))
        if mapped_user is not None:
            principal = Principal.user(mapped_user)
            sender = mapped_user
        elif adapter._allow_unauthenticated:
            # Explicit no-auth mode: the nick speaks only for itself.
            logger.warning(
                "IRC→mansio: unmapped nick %s publishing unauthenticated to %s",
                nick,
                mansio_channel,
            )
            principal = Principal.no_auth()
            sender = nick
        else:
            logger.warning(
                "IRC→mansio: dropping message from unmapped nick %s on %s",
                nick,
                irc_channel,
            )
            return

        logger.debug(
            "IRC→mansio: %s/%s: %s → %s",
            irc_channel,
            nick,
            text[:50],
            mansio_channel,
        )
        request = PublishRequest(
            channel=mansio_channel,
            sender=sender,
            msg_type="text",
            payload=text,
            metadata={"source": "irc", "irc_nick": nick},
        )
        try:
            message_operations.publish(operations, principal, request)
        except OperationError as exc:
            logger.warning(
                "IRC→mansio: publish rejected (%s): %s",
                exc.kind,
                exc.message,
            )

    def on_nick(self, connection, event):
        """Log nick changes that involve a mapped identity.

        Identity is looked up per message, so no state is carried across the
        change. A change *to* a mapped nick inherits that identity, which is
        sound only under the server's nick-ownership rules.
        """
        old_nick = event.source.nick
        new_nick = event.target
        adapter = self._adapter
        old_user = adapter._users.get(_casemap(old_nick))
        new_user = adapter._users.get(_casemap(new_nick))
        if old_user is None and new_user is None:
            return
        logger.info(
            "IRC nick change: %s → %s (mansio identity %s → %s)",
            old_nick,
            new_nick,
            old_user,
            new_user,
        )

    def on_disconnect(self, connection, event):
        """Handle disconnection from IRC server.

        Logs a warning, then delegates to the parent class which
        handles automatic reconnection with exponential backoff.
        """
        logger.warning("IRC bot disconnected from server")
        super().on_disconnect(connection, event)

    def on_nicknameinuse(self, connection, event):
        """Handle nickname collision by appending underscore."""
        new_nick = connection.get_nickname() + "_"
        logger.warning("Nickname in use, trying %s", new_nick)
        connection.nick(new_nick)


class IrcAdapter:
    """IRC adapter — bridges a mansio Bus to an IRC server.

    Maps IRC channels to mansio channels and forwards messages
    bidirectionally. Uses the ``irc`` PyPI package for protocol handling.

    Args:
        irc_host: IRC server hostname.
        irc_port: IRC server port. Default 6667.
        nickname: Bot nickname on IRC. Default "mansio-bot".
        channels: List of mansio channel names to bridge.
            Each is auto-mapped to ``#<name>`` on IRC.
        channel_map: Optional explicit mapping of mansio channel name
            to IRC channel name (e.g. ``{"tasks": "#project-tasks"}``).
            Overrides auto-mapping for listed channels.
        users: Mapping of IRC nick to mansio user id. Keys are folded with
            RFC1459 casemapping, whichever CASEMAPPING the server announces.
        token_store: Token store used when publishing an unmapped nick.
        allow_unauthenticated: Let an unmapped nick publish as itself when
            there is no token store. Default False; combining it with a
            token store raises ``ValueError``.
        password: Optional IRC server password.
        use_ssl: Use SSL/TLS for the IRC connection. Default False.

    Raises:
        ValueError: If a mapped user id is empty or invalid (the message
            names the IRC nick), if two nicks casemap to the same name, if a
            user id is mapped twice, if a nick is the bot's own, if a
            configured channel name is invalid, private-prefixed, or
            ``_system:``, or if ``allow_unauthenticated`` is combined with a
            token store.

    Example:
        >>> adapter = IrcAdapter(
        ...     irc_host="irc.example.com",
        ...     channels=["tasks", "sync"],
        ...     users={"alice": "alice-agent-1"},
        ... )
        >>> adapter.attach(bus)
        >>> adapter.serve_forever()  # blocks
    """

    def __init__(
        self,
        irc_host: str = "localhost",
        irc_port: int = 6667,
        nickname: str = "mansio-bot",
        channels: list[str] | None = None,
        channel_map: dict[str, str] | None = None,
        users: dict[str, str] | None = None,
        token_store: TokenStore | None = None,
        allow_unauthenticated: bool = False,
        password: str | None = None,
        use_ssl: bool = False,
    ) -> None:
        if allow_unauthenticated and token_store is not None:
            raise ValueError(
                "allow_unauthenticated cannot be combined with a token_store — "
                "drop the token store or the flag"
            )
        self._irc_host = irc_host
        self._irc_port = irc_port
        self._nickname = nickname
        self._password = password
        self._use_ssl = use_ssl
        self._token_store = token_store
        self._allow_unauthenticated = allow_unauthenticated
        self._bus: Bus | None = None
        self._operations: OperationContext | None = None
        self._bot: _IrcBridgeBot | None = None
        self._sub_ids: dict[str, str] = {}
        self._connection: Any | None = None
        self._revalidate_stop: threading.Event | None = None
        self._revalidate_thread: threading.Thread | None = None
        self._connection_lock = threading.Lock()

        self._users = self._build_user_map(users or {}, nickname)

        # Build channel mappings
        channels = channels or []
        channel_map = channel_map or {}
        for channel in list(channels) + list(channel_map):
            self._validate_bridge_channel(channel)

        # mansio channel → IRC channel
        self._mansio_to_irc: dict[str, str] = {}
        # IRC channel → mansio channel
        self._irc_to_mansio: dict[str, str] = {}

        for ch in channels:
            irc_ch = channel_map.get(ch, f"#{ch}")
            self._mansio_to_irc[ch] = irc_ch
            self._irc_to_mansio[irc_ch] = ch

        # Also add any extra mappings from channel_map not in channels
        for mansio_ch, irc_ch in channel_map.items():
            if mansio_ch not in self._mansio_to_irc:
                self._mansio_to_irc[mansio_ch] = irc_ch
                self._irc_to_mansio[irc_ch] = mansio_ch

    @staticmethod
    def _build_user_map(users: dict[str, str], nickname: str) -> dict[str, str]:
        """Validate *users* and key it by casemapped nick."""
        mapping: dict[str, str] = {}
        user_nicks: dict[str, str] = {}
        own_nick = _casemap(nickname)
        for nick, user_id in users.items():
            if not nick:
                raise ValueError("IRC nick mapping contains an empty nick")
            key = _casemap(nick)
            if key == own_nick:
                raise ValueError(f"IRC nick mapping must not map the bot's own nick {nick!r}")
            if key in mapping:
                raise ValueError(f"duplicate IRC nick mapping after casemapping: {nick!r}")
            if not is_valid_user_id(user_id):
                raise ValueError(f"invalid mansio user id for IRC nick {nick!r}: {user_id!r}")
            if user_id in user_nicks:
                raise ValueError(
                    f"mansio user id {user_id!r} is mapped to both "
                    f"{user_nicks[user_id]!r} and {nick!r}"
                )
            mapping[key] = user_id
            user_nicks[user_id] = nick
        return mapping

    def _validate_bridge_channel(self, channel: str) -> None:
        """Reject a configured channel name that the bridge can never use."""
        try:
            validate_channel_name(channel, token_store=self._token_store)
        except OperationError as exc:
            raise ValueError(
                f"invalid mansio channel name for IRC bridging {channel!r}: {exc.message}"
            ) from exc
        if is_private_channel(channel) or channel.startswith(_SYSTEM_PREFIX):
            raise ValueError(
                f"channel {channel!r} is private or system-scoped; IRC never bridges it"
            )

    def attach(self, bus: Bus) -> None:
        """Bind this adapter to a Bus.

        Args:
            bus: The Bus to expose over IRC.

        Raises:
            RuntimeError: If already attached.
        """
        if self._bus is not None:
            raise RuntimeError("Adapter already attached to a bus")
        self._bus = bus
        self._operations = OperationContext(
            bus=bus,
            authz=ChannelAuthorizer(bus),
            token_store=self._token_store,
            allow_unauthenticated=self._allow_unauthenticated,
        )

    def serve_forever(self) -> None:
        """Start the IRC bot. Blocks until shutdown().

        Connects to the IRC server, joins mapped channels, and begins
        bidirectional message bridging.

        Raises:
            RuntimeError: If not attached to a Bus.
        """
        if self._bus is None:
            raise RuntimeError("Must call attach(bus) before serve_forever()")

        server_list = [(self._irc_host, self._irc_port)]
        self._bot = _IrcBridgeBot(
            adapter=self,
            server_list=server_list,
            nickname=self._nickname,
            password=self._password,
        )

        logger.info(
            "Starting IRC adapter: %s:%d as %s, bridging %d channel(s)",
            self._irc_host,
            self._irc_port,
            self._nickname,
            len(self._mansio_to_irc),
        )

        # This blocks until disconnect
        self._bot.start()

    def shutdown(self) -> None:
        """Stop the IRC bot, the revalidation thread, and bus subscriptions.

        Safe to call multiple times.
        """
        # Stop checking before dropping the subscriptions it would revisit.
        self._stop_revalidation()
        if self._operations is not None:
            streams.unsubscribe(self._operations, list(self._sub_ids.values()))
            self._sub_ids.clear()

        # Disconnect IRC bot
        if self._bot is not None:
            with contextlib.suppress(Exception):
                self._bot.disconnect("mansio shutting down")
            with contextlib.suppress(Exception):
                self._bot.reactor.disconnect_all()
            self._bot = None

    @property
    def address(self) -> tuple[str, int]:
        """Return the (host, port) of the IRC server this adapter connects to."""
        return (self._irc_host, self._irc_port)

    def _setup_bus_subscriptions(self, connection) -> None:
        """Subscribe the bridgeable channels and forward their messages.

        Called after the bot connects to IRC and joins channels. Stops the
        previous revalidation thread and clears any existing subscriptions
        first, so a reconnect does not duplicate forwards.

        ``streams.bridgeable`` runs here, once per channel, and again every
        five seconds on a separate daemon thread. A publish that races a
        failing check can still be forwarded once; the next tick stops it.

        Args:
            connection: The IRC server connection object.
        """
        operations = self._operations
        if operations is None:
            return

        self._stop_revalidation()
        streams.unsubscribe(operations, list(self._sub_ids.values()))
        self._sub_ids.clear()
        self._connection = connection

        handles, skipped = streams.subscribe_bridged(
            operations,
            list(self._mansio_to_irc),
            lambda target, message: self._forward(connection, target, message),
        )
        self._sub_ids = handles

        for channel in skipped:
            logger.warning("Not bridging mansio channel '%s' to IRC: it is not public", channel)
        for channel in handles:
            logger.debug(
                "Subscribed to mansio channel '%s' → IRC %s",
                channel,
                self._mansio_to_irc[channel],
            )

        self._start_revalidation()

    def _forward(self, connection, mansio_channel: str, msg) -> None:
        """Write one mansio message to IRC.

        Runs on the publisher's thread, so the IRC connection is used under
        a lock: the reactor thread may be writing at the same time.
        """
        irc_channel = self._mansio_to_irc[mansio_channel]

        # Skip messages that originated from IRC (echo prevention)
        if msg.metadata and msg.metadata.get("source") == "irc":
            return

        # Format: "sender: payload"
        text = f"{msg.sender}: {msg.payload}"

        # Truncate by byte length to stay within IRC limits
        # while avoiding splitting multi-byte characters.
        encoded = text.encode("utf-8")
        if len(encoded) > _MAX_IRC_LINE:
            text = encoded[: _MAX_IRC_LINE - 3].decode("utf-8", errors="ignore") + "..."

        try:
            with self._connection_lock:
                connection.privmsg(irc_channel, text)
        except Exception:
            logger.exception("Failed to send message to IRC channel %s", irc_channel)

    def _start_revalidation(self) -> None:
        """Start the periodic visibility check for the current handles."""
        stop = threading.Event()
        thread = threading.Thread(
            target=self._revalidate_loop,
            args=(stop,),
            name="mansio-irc-revalidate",
            daemon=True,
        )
        self._revalidate_stop = stop
        self._revalidate_thread = thread
        thread.start()

    def _stop_revalidation(self) -> None:
        """Stop and join the revalidation thread, if one is running.

        A bounded join keeps a stuck backend read from blocking shutdown, but
        it also means the thread could still be running after this returns.
        Say so in the log rather than hiding it.
        """
        stop = self._revalidate_stop
        thread = self._revalidate_thread
        self._revalidate_stop = None
        self._revalidate_thread = None
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=_REVALIDATE_SECONDS + 1.0)
            if thread.is_alive():
                logger.warning(
                    "IRC revalidation thread still alive %.1fs after stop; "
                    "it will exit after its current check returns",
                    _REVALIDATE_SECONDS + 1.0,
                )

    def _revalidate_loop(self, stop: threading.Event) -> None:
        """Run :meth:`_revalidate_channels` every few seconds until stopped."""
        while not stop.wait(_REVALIDATE_SECONDS):
            self._revalidate_channels()

    def _revalidate_channels(self) -> None:
        """Drop channels that stop being bridgeable.

        A read error is not a revocation signal: the rule only ever narrows
        from public to private, so a transient failure keeps the channel
        until the next tick.

        An unsubscribe failure is not fatal either. The handle stays tracked
        and the channel keeps forwarding until a later tick can drop it;
        letting the error escape would kill the daemon thread instead, and a
        private channel would forward for the rest of the run.
        """
        operations = self._operations
        if operations is None:
            return
        for channel, sub_id in list(self._sub_ids.items()):
            if self._sub_ids.get(channel) != sub_id:
                continue
            try:
                allowed = streams.bridgeable(operations, channel)
            except Exception:
                logger.exception("IRC bridge revalidation failed for '%s'; keeping it", channel)
                continue
            if allowed:
                continue
            try:
                streams.unsubscribe(operations, [sub_id])
            except Exception:
                logger.exception(
                    "IRC bridge could not unsubscribe '%s'; keeping it for the next tick",
                    channel,
                )
                continue
            self._sub_ids.pop(channel, None)
            logger.warning("Stopped bridging mansio channel '%s': it is no longer public", channel)

    def __repr__(self) -> str:
        addr = f"{self._irc_host}:{self._irc_port}"
        status = "attached" if self._bus else "detached"
        n_channels = len(self._mansio_to_irc)
        return f"IrcAdapter({addr}, {self._nickname}, {n_channels} ch, {status})"
