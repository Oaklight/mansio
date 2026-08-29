"""mansio - Agent messaging hub for multi-agent collaboration."""

__version__ = "0.2.3"

from mansio.backends import MemoryBackend, SQLiteBackend
from mansio.backends.sqlite import SCHEMA_VERSION, SchemaVersionError, backup_database
from mansio.bus import Bus, SQLiteBus
from mansio.protocols import Backend, ChannelStore, Compactable, Deletable, Presenceable
from mansio.server import MansioServer
from mansio.system_policy import CompactionPolicy, system_channel_policy
from mansio.transport_http import HttpTransport, MansioAPIError
from mansio.types import (
    PERMISSION_LEVELS,
    ACLEntry,
    AgentPresence,
    ChannelMeta,
    ClaimResult,
    Message,
)

# Backward compatibility aliases — access triggers DeprecationWarning.
# Direct assignments removed; __getattr__ handles lazy warning on first use.

__all__ = [
    "ACLEntry",
    "PERMISSION_LEVELS",
    "AgentPresence",
    "Backend",
    "ChannelMeta",
    "ChannelStore",
    "Bus",
    "ClaimResult",
    "Compactable",
    "Deletable",
    "CompactionPolicy",
    "MaildirBackend",
    "NATSBackend",
    "HttpTransport",
    "MansioAPIError",
    "MemoryBackend",
    "MemoryStorage",
    "Message",
    "MansioServer",
    "Presenceable",
    "SCHEMA_VERSION",
    "SQLiteBackend",
    "SQLiteBus",
    "SQLiteStorage",
    "SchemaVersionError",
    "StorageBackend",
    "backup_database",
    "system_channel_policy",
]


def __getattr__(name: str):
    """Lazy import for optional backends and deprecated aliases."""
    import warnings

    # Deprecated aliases — will be removed in a future release.
    _DEPRECATED_ALIASES = {
        "SQLiteStorage": ("SQLiteBackend", SQLiteBackend),
        "MemoryStorage": ("MemoryBackend", MemoryBackend),
        "StorageBackend": ("Backend", Backend),
    }
    if name in _DEPRECATED_ALIASES:
        new_name, obj = _DEPRECATED_ALIASES[name]
        warnings.warn(
            f"mansio.{name} is deprecated, use mansio.{new_name} instead. "
            "It will be removed in mansio 1.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return obj

    # Optional backends (lazy-loaded).
    if name == "MaildirBackend":
        from mansio.backends.maildir import MaildirBackend

        return MaildirBackend
    if name == "NATSBackend":
        from mansio.backends.nats import NATSBackend

        return NATSBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
