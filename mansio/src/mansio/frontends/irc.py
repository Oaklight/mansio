"""IRC frontend for mansio — bridges a Bus to an IRC server.

Allows agents to communicate via IRC channels mapped to mansio channels.
IRC users can observe and participate in agent communication using any
standard IRC client.

Requires the ``irc`` PyPI package: ``pip install mansio[irc]``

Channel mapping:
    IRC ``#tasks`` ↔ mansio ``tasks`` (auto-strips/adds ``#`` prefix).
    Explicit mappings can override the default via *channel_map*.

Message flow:
    IRC → mansio: PRIVMSG in a mapped channel calls bus.publish().
    mansio → IRC: bus.subscribe() forwards new messages as IRC PRIVMSGs.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING

try:
    import irc.bot
    import irc.connection
    import irc.strings
except ImportError as exc:
    raise ImportError(
        "IrcFrontend requires the 'irc' package. Install it with: pip install mansio[irc]"
    ) from exc

if TYPE_CHECKING:
    from mansio.bus import Bus

logger = logging.getLogger(__name__)

# Maximum IRC message length (RFC 2812: 512 bytes including CRLF)
_MAX_IRC_LINE = 400  # leave room for protocol overhead


class _IrcBridgeBot(irc.bot.SingleServerIRCBot):
    """Internal IRC bot that bridges messages between IRC and a mansio Bus.

    Args:
        frontend: The owning IrcFrontend instance.
        server_list: IRC server list for SingleServerIRCBot.
        nickname: Bot nickname on IRC.
        password: Optional server password.
    """

    def __init__(
        self,
        frontend: IrcFrontend,
        server_list: list[tuple[str, int]],
        nickname: str,
        password: str | None = None,
    ) -> None:
        connect_params: dict = {}
        if frontend._use_ssl:
            connect_params["connect_factory"] = irc.connection.Factory(wrapper=self._ssl_wrapper)

        super().__init__(
            server_list,
            nickname,
            nickname,  # realname = nickname
            **connect_params,
        )
        self._frontend = frontend
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
        for irc_channel in self._frontend._mansio_to_irc.values():
            logger.info("Joining IRC channel %s", irc_channel)
            connection.join(irc_channel)

        # Set up bus subscriptions for mansio→IRC forwarding
        self._frontend._setup_bus_subscriptions(connection)

    def on_pubmsg(self, connection, event):
        """Handle public messages in IRC channels (IRC → mansio)."""
        irc_channel = event.target
        sender = event.source.nick
        text = event.arguments[0] if event.arguments else ""

        # Don't bridge our own messages
        if sender == connection.get_nickname():
            return

        mansio_channel = self._frontend._irc_to_mansio.get(irc_channel)
        if mansio_channel is None:
            return

        bus = self._frontend._bus
        if bus is None:
            return

        # Mark this as an IRC-originated message to prevent echo
        metadata = {"source": "irc", "irc_nick": sender}

        logger.debug(
            "IRC→mansio: %s/%s: %s → %s",
            irc_channel,
            sender,
            text[:50],
            mansio_channel,
        )
        bus.publish(
            channel=mansio_channel,
            sender=sender,
            msg_type="text",
            payload=text,
            metadata=metadata,
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


class IrcFrontend:
    """IRC frontend — bridges a mansio Bus to an IRC server.

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
        password: Optional IRC server password.
        use_ssl: Use SSL/TLS for the IRC connection. Default False.

    Example:
        >>> frontend = IrcFrontend(
        ...     irc_host="irc.example.com",
        ...     channels=["tasks", "sync"],
        ... )
        >>> frontend.attach(bus)
        >>> frontend.serve_forever()  # blocks
    """

    def __init__(
        self,
        irc_host: str = "localhost",
        irc_port: int = 6667,
        nickname: str = "mansio-bot",
        channels: list[str] | None = None,
        channel_map: dict[str, str] | None = None,
        password: str | None = None,
        use_ssl: bool = False,
    ) -> None:
        self._irc_host = irc_host
        self._irc_port = irc_port
        self._nickname = nickname
        self._password = password
        self._use_ssl = use_ssl
        self._bus: Bus | None = None
        self._bot: _IrcBridgeBot | None = None
        self._sub_ids: list[str] = []
        self._connection_lock = threading.Lock()

        # Build channel mappings
        channels = channels or []
        channel_map = channel_map or {}

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

    def attach(self, bus: Bus) -> None:
        """Bind this frontend to a Bus.

        Args:
            bus: The Bus to expose over IRC.

        Raises:
            RuntimeError: If already attached.
        """
        if self._bus is not None:
            raise RuntimeError("Frontend already attached to a bus")
        self._bus = bus

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
            frontend=self,
            server_list=server_list,
            nickname=self._nickname,
            password=self._password,
        )

        logger.info(
            "Starting IRC frontend: %s:%d as %s, bridging %d channel(s)",
            self._irc_host,
            self._irc_port,
            self._nickname,
            len(self._mansio_to_irc),
        )

        # This blocks until disconnect
        self._bot.start()

    def shutdown(self) -> None:
        """Stop the IRC bot and clean up bus subscriptions.

        Safe to call multiple times.
        """
        # Unsubscribe from bus channels
        if self._bus is not None:
            for sub_id in self._sub_ids:
                self._bus.unsubscribe(sub_id)
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
        """Return the (host, port) of the IRC server this frontend connects to."""
        return (self._irc_host, self._irc_port)

    def _setup_bus_subscriptions(self, connection) -> None:
        """Subscribe to mansio channels and forward messages to IRC.

        Called after the bot connects to IRC and joins channels.
        Clears any existing subscriptions first to prevent duplicates
        on reconnect (on_welcome fires again after auto-reconnect).

        Args:
            connection: The IRC server connection object.
        """
        if self._bus is None:
            return

        # Clear stale subscriptions from previous connection
        for old_sub_id in self._sub_ids:
            self._bus.unsubscribe(old_sub_id)
        self._sub_ids.clear()

        for mansio_ch, irc_ch in self._mansio_to_irc.items():

            def _make_callback(target_irc_ch: str):
                def _on_message(msg):
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
                        connection.privmsg(target_irc_ch, text)
                    except Exception:
                        logger.exception("Failed to send message to IRC channel %s", target_irc_ch)

                return _on_message

            sub_id = self._bus.subscribe(mansio_ch, _make_callback(irc_ch))
            self._sub_ids.append(sub_id)
            logger.debug("Subscribed to mansio channel '%s' → IRC %s", mansio_ch, irc_ch)

    def __repr__(self) -> str:
        addr = f"{self._irc_host}:{self._irc_port}"
        status = "attached" if self._bus else "detached"
        n_channels = len(self._mansio_to_irc)
        return f"IrcFrontend({addr}, {self._nickname}, {n_channels} ch, {status})"
