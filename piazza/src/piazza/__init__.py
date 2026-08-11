"""piazza - Agent messaging hub for multi-agent collaboration."""

__version__ = "0.2.3"

from piazza.backends import MemoryBackend, SQLiteBackend
from piazza.bus import Bus, SQLiteBus
from piazza.client import PiazzaClient
from piazza.protocols import Backend, MessageBus, Serializer
from piazza.serializers import JSONSerializer
from piazza.server import PiazzaServer
from piazza.transport_http import HttpTransport, PiazzaAPIError
from piazza.types import AgentPresence, ClaimResult, Message

# Backward compatibility aliases (deprecated, will be removed)
SQLiteStorage = SQLiteBackend
MemoryStorage = MemoryBackend
StorageBackend = Backend

__all__ = [
    "AgentPresence",
    "Backend",
    "Bus",
    "ClaimResult",
    "HttpTransport",
    "PiazzaAPIError",
    "JSONSerializer",
    "MemoryBackend",
    "MemoryStorage",
    "Message",
    "MessageBus",
    "PiazzaClient",
    "PiazzaServer",
    "SQLiteBackend",
    "SQLiteBus",
    "SQLiteStorage",
    "Serializer",
    "StorageBackend",
]
