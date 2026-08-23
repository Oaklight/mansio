"""Base protocol for server-side frontends.

A Frontend is a protocol adapter that exposes a Bus over a specific
transport protocol (HTTP, WebSocket, IRC, etc.). Multiple frontends
can attach to the same Bus, allowing different client types to
coexist on one MansioServer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mansio.bus import Bus


class Frontend(Protocol):
    """Server-side protocol adapter interface.

    Implementations expose the Bus's publish/query/subscribe
    capabilities over a specific wire protocol.

    Lifecycle:
        1. attach(bus) — bind to a Bus instance
        2. serve_forever() — start accepting connections (blocking)
        3. shutdown() — stop accepting, clean up

    Example:
        >>> frontend = HttpFrontend(host="0.0.0.0", port=8741)
        >>> frontend.attach(bus)
        >>> frontend.serve_forever()  # blocks
    """

    def attach(self, bus: Bus) -> None:
        """Bind this frontend to a Bus instance.

        Must be called before serve_forever(). The frontend will
        delegate all publish/query/subscribe operations to this bus.

        Args:
            bus: The Bus to expose.

        Raises:
            RuntimeError: If already attached.
        """
        ...

    def serve_forever(self) -> None:
        """Start accepting client connections. Blocks until shutdown().

        Raises:
            RuntimeError: If not attached to a Bus.
        """
        ...

    def shutdown(self) -> None:
        """Stop accepting connections and release resources.

        Safe to call multiple times. After shutdown, the frontend
        cannot be restarted — create a new instance instead.
        """
        ...

    @property
    def address(self) -> tuple[str, int]:
        """Return the (host, port) this frontend is listening on.

        Raises:
            RuntimeError: If not yet serving.
        """
        ...
