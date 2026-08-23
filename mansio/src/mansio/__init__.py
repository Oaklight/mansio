"""mansio - Agent messaging hub for multi-agent collaboration."""

__version__ = "0.2.3"

from mansio.backends import MemoryBackend, SQLiteBackend
from mansio.bus import Bus, SQLiteBus
from mansio.client import MansioClient
from mansio.protocols import Backend, Compactable, MessageBus, Presenceable, Serializer
from mansio.serializers import JSONSerializer
from mansio.server import MansioServer
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
    "HttpTransport",
    "MansioAPIError",
    "JSONSerializer",
    "MemoryBackend",
    "MemoryStorage",
    "Message",
    "MessageBus",
    "MansioClient",
    "MansioServer",
    "Presenceable",
    "SQLiteBackend",
    "SQLiteBus",
    "SQLiteStorage",
    "Serializer",
    "StorageBackend",
]
