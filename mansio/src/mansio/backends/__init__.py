"""Pluggable message backends for mansio bus."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mansio.backends.memory import MemoryBackend
from mansio.backends.sqlite import SQLiteBackend

if TYPE_CHECKING:
    from mansio.backends.nats import NATSBackend

__all__ = ["MemoryBackend", "NATSBackend", "SQLiteBackend"]


def __getattr__(name: str):
    """Lazy import for optional backends."""
    if name == "NATSBackend":
        from mansio.backends.nats import NATSBackend

        return NATSBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
