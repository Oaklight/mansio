"""MansioServer — orchestrates Bus + Frontend(s).

Central server process that holds a Bus instance and exposes it
through one or more Frontend protocol adapters.

Example:
    >>> from mansio import Bus, SQLiteBackend
    >>> from mansio.frontends import HttpFrontend
    >>> from mansio.server import MansioServer
    >>>
    >>> bus = Bus(backend=SQLiteBackend("mansio.db"))
    >>> server = MansioServer(bus)
    >>> server.add_frontend(HttpFrontend(port=8741))
    >>> server.serve_forever()  # blocks
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mansio.bus import Bus
    from mansio.frontends.base import Frontend


class MansioServer:
    """Orchestrator that binds a Bus to one or more Frontends.

    Args:
        bus: The Bus instance to serve.
    """

    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._frontends: list[Frontend] = []
        self._threads: list[threading.Thread] = []

    def add_frontend(self, frontend: Frontend) -> None:
        """Attach a frontend to the bus.

        Args:
            frontend: A Frontend implementation to add.

        Raises:
            RuntimeError: If server is already running.
        """
        if self._threads:
            raise RuntimeError("Cannot add frontends while server is running")
        frontend.attach(self._bus)
        self._frontends.append(frontend)

    def serve_forever(self) -> None:
        """Start all frontends. Blocks until shutdown().

        If only one frontend, runs in the calling thread.
        If multiple, each gets its own thread; calling thread blocks.

        Raises:
            RuntimeError: If no frontends added.
        """
        if not self._frontends:
            raise RuntimeError("No frontends added. Call add_frontend() first.")

        if len(self._frontends) == 1:
            # Single frontend — run in calling thread
            self._frontends[0].serve_forever()
            return

        # Multiple frontends — each in its own thread
        for i, fe in enumerate(self._frontends):
            t = threading.Thread(
                target=fe.serve_forever,
                name=f"mansio-frontend-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        # Block until interrupted
        try:
            for t in self._threads:
                t.join()
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self) -> None:
        """Stop all frontends and release resources."""
        for fe in self._frontends:
            fe.shutdown()
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()
        self._bus.close()

    @property
    def bus(self) -> Bus:
        """The underlying Bus instance."""
        return self._bus

    def __repr__(self) -> str:
        fe_names = [type(f).__name__ for f in self._frontends]
        return f"MansioServer(frontends={fe_names})"
