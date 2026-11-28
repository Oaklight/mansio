"""MansioServer — orchestrates Bus + ServerAdapter(s).

Central server process that holds a Bus instance and exposes it
through one or more ServerAdapter implementations.

Example:
    >>> from mansio_core import Bus, SQLiteBackend
    >>> from mansio_web.adapters import HttpAdapter
    >>> from mansio_web.server import MansioServer
    >>> from mansio_core.token_store import TokenStore
    >>>
    >>> bus = Bus(backend=SQLiteBackend("mansio.db"))
    >>> tokens = TokenStore("mansio.db")
    >>> server = MansioServer(bus)
    >>> server.add_adapter(HttpAdapter(port=8741, token_store=tokens))
    >>> server.serve_forever()  # blocks
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mansio_core.bus import Bus
    from mansio_core.server import ServerAdapter


class MansioServer:
    """Orchestrator that binds a Bus to one or more ServerAdapters.

    Args:
        bus: The Bus instance to serve.
    """

    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self._adapters: list[ServerAdapter] = []
        self._threads: list[threading.Thread] = []

    def add_adapter(self, adapter: ServerAdapter) -> None:
        """Attach an adapter to the bus.

        Args:
            adapter: A ServerAdapter implementation to add.

        Raises:
            RuntimeError: If server is already running.
        """
        if self._threads:
            raise RuntimeError("Cannot add adapters while server is running")
        adapter.attach(self._bus)
        self._adapters.append(adapter)

    def serve_forever(self) -> None:
        """Start all adapters. Blocks until shutdown().

        If only one adapter, runs in the calling thread.
        If multiple, each gets its own thread; calling thread blocks.

        Raises:
            RuntimeError: If no adapters added.
        """
        if not self._adapters:
            raise RuntimeError("No adapters added. Call add_adapter() first.")

        if len(self._adapters) == 1:
            # Single adapter — run in calling thread
            self._adapters[0].serve_forever()
            return

        # Multiple adapters — each in its own thread
        for i, adapter in enumerate(self._adapters):
            t = threading.Thread(
                target=adapter.serve_forever,
                name=f"mansio-adapter-{i}",
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
        """Stop all adapters and release resources."""
        for adapter in self._adapters:
            adapter.shutdown()
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()
        self._bus.close()

    @property
    def bus(self) -> Bus:
        """The underlying Bus instance."""
        return self._bus

    def __repr__(self) -> str:
        adapter_names = [type(f).__name__ for f in self._adapters]
        return f"MansioServer(adapters={adapter_names})"
