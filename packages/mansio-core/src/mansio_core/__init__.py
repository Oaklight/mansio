"""Core types, protocols, bus, backends and authorization for mansio.

This package has no runtime dependencies and no knowledge of HTTP or the
admin panel.  Network adapters live in :mod:`mansio_web`.
"""

from __future__ import annotations

__version__ = "0.3.0.dev1"

from mansio_core.backends import MemoryBackend, SQLiteBackend
from mansio_core.backends.sqlite import SCHEMA_VERSION, SchemaVersionError, backup_database
from mansio_core.bus import Bus, SQLiteBus
from mansio_core.metrics import MetricsCollector
from mansio_core.protocols import (
    Backend,
    ChannelStore,
    Compactable,
    Deletable,
    Presenceable,
)
from mansio_core.system_policy import CompactionPolicy, system_channel_policy
from mansio_core.types import (
    ALLOWED_PERMISSIONS,
    PERMISSION_LEVELS,
    ROLE_PRESETS,
    VERBS,
    ACLEntry,
    ChannelMeta,
    ClaimResult,
    Message,
    UserPresence,
    permission_allows,
)

__all__ = [
    "ALLOWED_PERMISSIONS",
    "ACLEntry",
    "PERMISSION_LEVELS",
    "ROLE_PRESETS",
    "UserPresence",
    "VERBS",
    "Backend",
    "ChannelMeta",
    "ChannelStore",
    "Bus",
    "ClaimResult",
    "Compactable",
    "Deletable",
    "CompactionPolicy",
    "MaildirBackend",
    "MetricsCollector",
    "NATSBackend",
    "MemoryBackend",
    "MemoryStorage",
    "Message",
    "Presenceable",
    "SCHEMA_VERSION",
    "SQLiteBackend",
    "SQLiteBus",
    "SQLiteStorage",
    "SchemaVersionError",
    "StorageBackend",
    "backup_database",
    "permission_allows",
    "system_channel_policy",
]


_DEPRECATED_ALIASES = {
    "SQLiteStorage": ("SQLiteBackend", SQLiteBackend),
    "MemoryStorage": ("MemoryBackend", MemoryBackend),
    "StorageBackend": ("Backend", Backend),
}


def __getattr__(name: str):
    """Lazy import for optional backends and deprecated aliases."""
    import warnings

    if name in _DEPRECATED_ALIASES:
        new_name, obj = _DEPRECATED_ALIASES[name]
        warnings.warn(
            f"mansio_core.{name} is deprecated, use mansio_core.{new_name} instead. "
            "It will be removed in mansio 1.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return obj

    if name == "MaildirBackend":
        from mansio_core.backends.maildir import MaildirBackend

        return MaildirBackend
    if name == "NATSBackend":
        from mansio_core.backends.nats import NATSBackend

        return NATSBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
