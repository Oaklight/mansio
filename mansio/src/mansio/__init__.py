"""mansio - Agent messaging hub for multi-agent collaboration."""

__version__ = "0.2.3"

from mansio.backends import MemoryBackend, SQLiteBackend
from mansio.backends.sqlite import SCHEMA_VERSION, SchemaVersionError, backup_database
from mansio.bus import Bus, SQLiteBus
from mansio.client import MansioClient
from mansio.protocols import Backend, Compactable, Deletable, Presenceable
from mansio.server import MansioServer
from mansio.system_policy import CompactionPolicy, system_channel_policy
from mansio.transport_http import HttpTransport, MansioAPIError
from mansio.types import AgentPresence, ClaimResult, Message

# Backward compatibility aliases (deprecated, will be removed)
SQLiteStorage = SQLiteBackend
MemoryStorage = MemoryBackend
StorageBackend = Backend

__all__ = [
    "AgentPresence",
    "Backend",
    "Bus",
    "ClaimResult",
    "Compactable",
    "Deletable",
    "CompactionPolicy",
    "MaildirBackend",
    "HttpTransport",
    "MansioAPIError",
    "MemoryBackend",
    "MemoryStorage",
    "Message",
    "MansioClient",
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
