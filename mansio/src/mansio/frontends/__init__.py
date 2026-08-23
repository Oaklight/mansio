"""Server-side frontend adapters for mansio.

Frontends expose a Bus over different protocols (HTTP, WebSocket, IRC, etc.).
Multiple frontends can attach to the same Bus simultaneously.
"""

from mansio.frontends.base import Frontend
from mansio.frontends.http import HttpFrontend


def __getattr__(name: str):
    """Lazy import for optional frontends (avoids hard dependency on extras)."""
    if name == "IrcFrontend":
        from mansio.frontends.irc import IrcFrontend

        return IrcFrontend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["Frontend", "HttpFrontend", "IrcFrontend"]
