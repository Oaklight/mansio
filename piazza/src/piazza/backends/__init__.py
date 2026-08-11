"""Pluggable message backends for piazza bus."""

from piazza.backends.memory import MemoryBackend
from piazza.backends.sqlite import SQLiteBackend

__all__ = ["MemoryBackend", "NATSBackend", "SQLiteBackend"]


def __getattr__(name: str):
    """Lazy import for optional backends."""
    if name == "NATSBackend":
        from piazza.backends.nats import NATSBackend

        return NATSBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
