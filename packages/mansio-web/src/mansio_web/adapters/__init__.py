"""Server-side adapters for mansio.

Adapters expose a Bus over different protocols (HTTP, WebSocket, IRC, etc.).
Multiple adapters can attach to the same Bus simultaneously.
"""

from mansio_web.adapters.base import ServerAdapter
from mansio_web.adapters.http import HttpAdapter


def __getattr__(name: str):
    """Lazy import for optional adapters (avoids hard dependency on extras)."""
    if name == "IrcAdapter":
        from mansio_web.adapters.irc import IrcAdapter

        return IrcAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ServerAdapter", "HttpAdapter", "IrcAdapter"]
